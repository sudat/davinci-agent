"""Stdio JSON-RPC probe transport for the pinned davinci-resolve-mcp server.

Owns the process lifecycle concerns of one probe session: spawn the pinned
entry point in its own process group with update checks disabled, send a
JSON-RPC ``initialize``, then (only once the server answers) a
``tools/call`` Resolve-version probe, reading LF-delimited replies under a
hard deadline, and always tear the process group down afterwards.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Literal

JSONRPCMessage = dict[str, object]

KILL_GRACE_SECONDS: Final = 5.0
PROBE_ENV: Final = {
    "DAVINCI_RESOLVE_MCP_UPDATE_CHECK": "0",
    "PYTHONUTF8": "1",
    "PYTHONUNBUFFERED": "1",
}


@dataclass(frozen=True, slots=True)
class StdioProbe:
    """One probe session's observation; ``failure`` is "" when both replies arrived."""

    initialize_response: JSONRPCMessage | None
    resolve_response: JSONRPCMessage | None
    failure: Literal["", "exit", "timeout"]
    exit_code: int | None
    timeout_seconds: float


def read_jsonrpc_line(stdout: IO[bytes], deadline: float) -> JSONRPCMessage:
    """Read LF-delimited JSON-RPC lines until one parses, or the deadline passes."""
    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            for _key, _mask in selector.select(timeout=max(remaining, 0.0)):
                chunk = os.read(stdout.fileno(), 65536)
                if not chunk:
                    return {}
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        return parsed
        return {}
    finally:
        selector.close()


def stop_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the probe's whole process group (the server may have children)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=KILL_GRACE_SECONDS)


def _request(method: str, request_id: int, params: Mapping[str, object]) -> JSONRPCMessage:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}


def probe_stdio_server(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    resolve_tool: str,
    resolve_arguments: Mapping[str, object],
) -> StdioProbe:
    """Send ``initialize`` then one Resolve-version tool call over stdio."""
    env = {**os.environ, **PROBE_ENV}
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
    )
    stdin_pipe = proc.stdin
    stdout_pipe = proc.stdout
    if stdin_pipe is None or stdout_pipe is None:
        stop_process_group(proc)
        return StdioProbe(None, None, "exit", proc.returncode, timeout_seconds)
    initialize_request = _request(
        "initialize",
        1,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-doctor", "version": "mcp-doctor-v1"},
        },
    )
    try:
        stdin_pipe.write(json.dumps(initialize_request).encode("utf-8") + b"\n")
        stdin_pipe.flush()
        initialize_response = read_jsonrpc_line(stdout_pipe, time.monotonic() + timeout_seconds)
        if not initialize_response:
            failure: Literal["exit", "timeout"] = (
                "exit" if proc.poll() is not None else "timeout"
            )
            return StdioProbe(None, None, failure, proc.returncode, timeout_seconds)
        resolve_request = _request(
            "tools/call", 2, {"name": resolve_tool, "arguments": dict(resolve_arguments)}
        )
        stdin_pipe.write(json.dumps(resolve_request).encode("utf-8") + b"\n")
        stdin_pipe.flush()
        resolve_response = read_jsonrpc_line(stdout_pipe, time.monotonic() + timeout_seconds)
        return StdioProbe(initialize_response, resolve_response or None, "", None, timeout_seconds)
    finally:
        stdin_pipe.close()
        stop_process_group(proc)
        stdout_pipe.close()


__all__ = [
    "JSONRPCMessage",
    "StdioProbe",
    "probe_stdio_server",
    "read_jsonrpc_line",
    "stop_process_group",
]
