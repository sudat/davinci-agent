"""Stdio JSON-RPC 2.0 transport for the pinned MCP server subprocess.

Lifecycle lessons inherited from ``services/cli/mcp_probe.py`` (task 2):
spawn in its own session (process group), read only under an explicit
deadline, and tear the whole group down SIGTERM->SIGKILL with pipes closed
in ``finally`` (``filterwarnings = ["error"]`` turns GC'd unclosed pipes
into test failures). Unlike the probe this transport is parameterized:
command, cwd, and env come from :class:`StdioTransportConfig` — buildable
from the pin contract via :meth:`StdioTransportConfig.from_pin` — never
hardwired to mcp-doctor.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import IO, Final, Self

from services.mcp_client.errors import McpJsonRpcError, McpTimeoutError, McpTransportError
from services.mcp_client.transport_config import (
    DEFAULT_KILL_GRACE_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_SURFACE_COVERAGE_DIR,
    PinSurfaceContext,
    StdioTransportConfig,
)

JSONRPCMessage = dict[str, object]

_READ_CHUNK_BYTES: Final = 65536
_INTERNAL_ERROR_CODE: Final = -32603


class StdioJsonRpcTransport:
    """Sequential request/response transport over the server's stdio pipes.

    Single-writer by construction: one request is in flight at a time, each
    response correlated by request id under an explicit timeout.
    """

    def __init__(self, config: StdioTransportConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: IO[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._next_request_id = 1
        self._closed = False

    @property
    def config(self) -> StdioTransportConfig:
        """The launch spec (including any pin surface context) this transport owns."""
        return self._config

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        """The server subprocess, or ``None`` before :meth:`start`."""
        return self._process

    def start(self) -> None:
        """Spawn the server subprocess in its own process group."""
        if self._process is not None:
            raise McpTransportError("transport already started")
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            **self._config.extra_env,
        }
        process = subprocess.Popen(
            self._config.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=None if self._config.cwd is None else str(self._config.cwd),
            env=env,
            start_new_session=True,
        )
        self._process = process
        if process.stdin is None or process.stdout is None:
            self.close()
            raise McpTransportError("failed to open stdio pipes to the server")
        self._stdin = process.stdin
        self._stdout = process.stdout

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JSONRPCMessage:
        """Send one request and return its id-correlated response object."""
        timeout = (
            self._config.request_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {} if params is None else dict(params),
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            message = self._read_message(method, timeout, deadline)
            if message.get("id") != request_id:
                continue  # stale or unrelated line: keep waiting for ours
            error = message.get("error")
            if isinstance(error, dict):
                raw_code = error.get("code")
                raw_message = error.get("message")
                raise McpJsonRpcError(
                    method,
                    code=raw_code if isinstance(raw_code, int) else _INTERNAL_ERROR_CODE,
                    message=(
                        raw_message if isinstance(raw_message, str) else "unknown error"
                    ),
                )
            return message

    def send_notification(
        self, method: str, params: Mapping[str, object] | None = None
    ) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": {} if params is None else dict(params),
            }
        )

    def close(self) -> None:
        """Stop the server process group and close both pipes (idempotent)."""
        if self._closed:
            return
        self._closed = True
        stdin, stdout, process = self._stdin, self._stdout, self._process
        try:
            if stdin is not None:
                stdin.close()
                self._stdin = None
        finally:
            try:
                if process is not None:
                    self._stop_process_group(process)
            finally:
                if stdout is not None:
                    stdout.close()
                    self._stdout = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _stop_process_group(self, process: subprocess.Popen[bytes]) -> None:
        """SIGTERM the whole process group, then SIGKILL after the grace."""
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=self._config.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            process.wait(timeout=self._config.kill_grace_seconds)

    def _write(self, payload: Mapping[str, object]) -> None:
        stdin, _ = self._require_pipes()
        try:
            stdin.write(json.dumps(dict(payload)).encode("utf-8") + b"\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpTransportError(f"failed writing to server stdin: {exc}") from exc

    def _read_message(
        self, method: str, timeout_seconds: float, deadline: float
    ) -> JSONRPCMessage:
        """Read until one JSON object line arrives; EOF/timeout raise typed."""
        _, stdout = self._require_pipes()
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ)
        buffer = b""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpTimeoutError(method, timeout_seconds)
                events = selector.select(timeout=remaining)
                if not events:
                    continue  # deadline re-checked on the next pass
                chunk = os.read(stdout.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    raise McpTransportError("server closed its stdout")
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    try:
                        parsed: object = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate junk lines, keep the deadline
                    if isinstance(parsed, dict):
                        return parsed
        except OSError as exc:
            raise McpTransportError(f"failed reading server stdout: {exc}") from exc
        finally:
            selector.close()

    def _require_pipes(self) -> tuple[IO[bytes], IO[bytes]]:
        if self._closed or self._stdin is None or self._stdout is None:
            raise McpTransportError(
                "transport is not available (not started or already closed)"
            )
        return self._stdin, self._stdout


__all__ = [
    "DEFAULT_KILL_GRACE_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_SURFACE_COVERAGE_DIR",
    "JSONRPCMessage",
    "McpJsonRpcError",
    "McpTimeoutError",
    "McpTransportError",
    "PinSurfaceContext",
    "StdioJsonRpcTransport",
    "StdioTransportConfig",
]
