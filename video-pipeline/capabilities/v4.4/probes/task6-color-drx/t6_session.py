# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 6 live-probe session bootstrap + calibrated synthetic media.

Imports the live client stack (pinned MCP client, recorder, product
adapter, pinned QC tools); only the live run pulls this module in.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
SCRATCH = EVIDENCE / ".scratch"
RENDER_DIR = EVIDENCE / "render"
LEDGER_DIR = EVIDENCE / "ledger"
PIN_PATH = VIDEO_PIPELINE / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"

#: Per-run disposable project: a stale prior run can never be resumed.
RUN_TOKEN = uuid4().hex[:6]
PROJECT_NAME = f"probe-t6color-{RUN_TOKEN}"
TIMELINE_NAME = f"{PROJECT_NAME}-timeline"

#: Cross-probe state bag.
ProbeState = dict[str, Any]


def reset_evidence_tree() -> None:
    for path in (LEDGER_DIR, RENDER_DIR, SCRATCH):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)


def generate_calibrated_clip() -> Path:
    sys.path.insert(0, str(VIDEO_PIPELINE))
    from services.qc.tools import load_qc_tools  # noqa: PLC0415 (post-sys.path bootstrap)

    tools = load_qc_tools()
    clip = SCRATCH / "t6-source.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=30:d=4",
            "-c:v",
            "h264_videotoolbox",
            "-g",
            "30",
            str(clip),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0 or not clip.is_file():
        raise RuntimeError(f"calibrated clip generation failed: {result.stderr[-300:]}")
    return clip


def open_session() -> ProbeState:
    sys.path.insert(0, str(VIDEO_PIPELINE))
    from services.mcp_client.call_models import (  # noqa: PLC0415 (post-bootstrap)
        McpCallRecorder,
        append_call_record,
    )
    from services.mcp_client.client import McpClient  # noqa: PLC0415 (post-bootstrap)
    from services.mcp_client.transport import (  # noqa: PLC0415 (post-bootstrap)
        StdioJsonRpcTransport,
        StdioTransportConfig,
    )
    from services.toolchain.mcp_pin import load_mcp_pin  # noqa: PLC0415 (post-bootstrap)

    pin = load_mcp_pin(PIN_PATH)
    config = StdioTransportConfig.from_pin(pin, request_timeout_seconds=60.0)
    client = McpClient(StdioJsonRpcTransport(config))
    client.connect()
    version = client.resolve_get_version()
    recorder = McpCallRecorder(
        provider_version=version.mcp.version,
        resolve_version=version.version_string,
        server_mode=pin.server_mode,
    )
    original_call_tool = client._call_tool  # noqa: SLF001 (evidence capture seam)

    def recording_call_tool(name: str, arguments: dict, **kwargs: object) -> object:
        started = int(time.time())
        response_text = None
        try:
            result = original_call_tool(name, arguments, **kwargs)
            response_text = result.text
            return result
        finally:
            action = arguments.get("action", "-") if isinstance(arguments, dict) else "-"
            params = arguments.get("params", {}) if isinstance(arguments, dict) else {}
            record = recorder.build_record(
                tool_name=name,
                action=str(action),
                normalized_params=dict(params) if isinstance(params, dict) else {},
                request_payload={"name": name, "arguments": dict(arguments)},
                response_payload=response_text or {"error": "no-response"},
                status="ok",
                started_at=started,
                finished_at=max(int(time.time()), started),
            )
            append_call_record(record, LEDGER_DIR)

    client._call_tool = recording_call_tool  # noqa: SLF001 (same capture seam)
    return {
        "client": client,
        "resolve_version": version.version_string,
        "provider_version": version.mcp.version,
    }


def close_session(state: ProbeState) -> None:
    client = state["client"]
    with contextlib.suppress(Exception):
        client._call_action_json(  # noqa: SLF001 (scoped cleanup)
            "project_manager", "delete", {"name": PROJECT_NAME}
        )
    client.close()


def scrub_repo_prefixes() -> None:
    prefix = f"{VIDEO_PIPELINE}/"
    for path in sorted(EVIDENCE.glob("*.json")):
        text = path.read_text()
        if prefix in text:
            path.write_text(text.replace(prefix, ""))


__all__ = [
    "EVIDENCE",
    "LEDGER_DIR",
    "PIN_PATH",
    "PROJECT_NAME",
    "RENDER_DIR",
    "RUN_TOKEN",
    "SCRATCH",
    "TIMELINE_NAME",
    "VIDEO_PIPELINE",
    "ProbeState",
    "close_session",
    "generate_calibrated_clip",
    "open_session",
    "reset_evidence_tree",
    "scrub_repo_prefixes",
]
