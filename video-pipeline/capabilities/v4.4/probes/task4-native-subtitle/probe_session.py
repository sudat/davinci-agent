# noqa: INP001 (evidence tree is not an importable package by design)
"""Live session bootstrap and committed-cue fixtures for the Task 4 probe.

Importing this module loads the live client stack (pinned MCP client,
recorder, parity media) — only the live run pulls it in. The committed
cues below are the exact text/timing contract the native subtitle mutation
must reproduce; ``params_for`` shapes them into the product payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import live_support
from evidence_io import SCRATCH

from services.mcp_client.call_models import McpCallRecorder
from services.mcp_client.client import McpClient
from services.mcp_client.ops import McpOps
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig
from services.qa.parity_mcp import generate_parity_media
from services.toolchain.mcp_pin import load_mcp_pin

if TYPE_CHECKING:
    from live_support import LiveSession

#: The Japanese-capable font the default style profile binds.
BOUND_FONT = "Hiragino Sans W3"

CUE1 = {"cue_id": "cue-s1", "text": "今日はDaVinci Resolveの使い方を紹介します",
        "start_frame": 15, "end_frame": 45}
CUE2 = {"cue_id": "cue-s2", "text": "再生と編集の違いに注意してください",
        "start_frame": 65, "end_frame": 86}
CUE3_LONG = {
    "cue_id": "cue-s3-long",
    "text": "これは長い字幕の確認用キューで、複数行の日本語テキストが\n"
            "フレームの右端で切れずに読みやすく表示されることを確認します",
    "start_frame": 100,
    "end_frame": 150,
}
CUES = (CUE1, CUE2, CUE3_LONG)
CONTROL_FRAMES = (5, 55, 90)
CUE_FRAMES = {"cue-s1": 30, "cue-s2": 75, "cue-s3-long": 125}

#: Cross-probe mutable state bag (adapter handle, timeline ids, clip id).
ProbeState = dict[str, Any]


def open_session() -> LiveSession:
    pin = load_mcp_pin(live_support.PIN_PATH)
    config = StdioTransportConfig.from_pin(pin, request_timeout_seconds=60.0)
    client = McpClient(StdioJsonRpcTransport(config))
    client.connect()
    media = generate_parity_media(SCRATCH)
    version = client.resolve_get_version()
    session = live_support.LiveSession(
        ops=None,  # type: ignore[arg-type]
        client=client,
        media=media,
        project_name="probe-t4final",
        resolve_version_string=version.version_string,
    )
    session.ops = McpOps(client)
    recorder = McpCallRecorder(
        provider_version=version.mcp.version,
        resolve_version=version.version_string,
        server_mode=pin.server_mode,
    )
    client._call_tool = live_support._CapturingCallTool(client._call_tool, session, recorder)  # noqa: SLF001 (evidence capture seam: the recorder must wrap the client's private call hook to ledger every vendor call)
    return session


def params_for(cues: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "action": "apply_subtitles",
        "selected_path": "native_text_plus",
        "cues": [
            {
                "cue_id": str(cue["cue_id"]),
                "text": str(cue["text"]),
                "record_span": {
                    "start_frame": cue["start_frame"],
                    "end_frame": cue["end_frame"],
                },
            }
            for cue in cues
        ],
        "style_profile_id": "subtitle-style-default",
    }


__all__ = [
    "BOUND_FONT",
    "CONTROL_FRAMES",
    "CUE1",
    "CUE2",
    "CUE3_LONG",
    "CUES",
    "CUE_FRAMES",
    "ProbeState",
    "open_session",
    "params_for",
]
