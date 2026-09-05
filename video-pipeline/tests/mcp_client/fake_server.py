"""Fake stdio MCP server for ``services.mcp_client`` unit tests.

A minimal JSON-RPC 2.0 responder intended ONLY as a test subprocess: it
answers ``initialize`` / ``tools/list`` / ``tools/call`` from canned
results, one LF-delimited JSON object per line. Test modes via environment
variables (read once at spawn):

- ``FAKE_MCP_SERVER_NAME`` / ``FAKE_MCP_SERVER_VERSION`` override the
  reported ``serverInfo`` — set a foreign name for identity-mismatch mode.
- ``FAKE_MCP_DELAY_SECONDS`` sleeps before answering ``tools/call`` —
  set it far above any client timeout for the hung-server mode.

``tools/call`` arguments follow the compound server convention
``{"action": ..., "params": {...}`` and canned actions mirror the LIVE
payload shapes recorded in task 11 (resolve version, import, structure
snapshot, render job lifecycle) so the typed ops surface is exercised
against realistic wire data.
"""

from __future__ import annotations

import json
import os
import sys
import time

SERVER_NAME = os.environ.get("FAKE_MCP_SERVER_NAME", "DaVinciResolveMCP")
SERVER_VERSION = os.environ.get("FAKE_MCP_SERVER_VERSION", "1.29.1")
TOOL_DELAY_SECONDS = float(os.environ.get("FAKE_MCP_DELAY_SECONDS", "0"))

CANNED_TOOLS = [
    {
        "name": "resolve_control",
        "description": "resolve control surface (fake)",
        "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}},
    },
    {
        "name": "echo",
        "description": "echo arguments back as JSON text (fake)",
        "inputSchema": {"type": "object"},
    },
]

VERSION_PAYLOAD = {
    "product": "DaVinci Resolve Studio",
    "version": [21, 0, 4, 5, ""],
    "version_string": "21.0.4.5",
    "build": {
        "unavailable_on_this_build": [],
        "known_gates": 41,
        "note": "fake server canned payload",
    },
    "mcp": {
        "version": "2.207.0",
        "update": {
            "status": "disabled",
            "current_version": "2.207.0",
            "update_mode": "never",
            "checked_at": 1787374787.160726,
        },
        "update_decision": {"action": "none", "reason": "no_update", "update_mode": "never"},
    },
}

STRUCTURE_SNAPSHOT = {
    "name": "Fake Timeline",
    "id": "tl-fake-1",
    "start_frame": 108000,
    "end_frame": 108210,
    "start_timecode": "01:00:00:00",
    "item_count": 2,
    "tracks": {
        "video": {
            "track_count": 1,
            "tracks": [
                {
                    "track_index": 1,
                    "item_count": 1,
                    "items": [
                        {
                            "name": "parity-src-001.mp4",
                            "timeline_item_id": "ti-v1",
                            "track_type": "video",
                            "track_index": 1,
                            "item_index": 0,
                            "start": 108000,
                            "end": 108060,
                            "duration": 60,
                            "source_start": 0,
                            "source_end": 60,
                            "source_fps": 30.0,
                            "media_pool_item_id": "mpi-1",
                            "media_pool_item_name": "parity-src-001",
                            "file_path": "media/parity-src-001.mp4",
                        }
                    ],
                }
            ],
        },
        "audio": {
            "track_count": 1,
            "tracks": [
                {
                    "track_index": 1,
                    "item_count": 1,
                    "items": [
                        {
                            "name": "parity-src-001.mp4",
                            "timeline_item_id": "ti-a1",
                            "track_type": "audio",
                            "track_index": 1,
                            "item_index": 0,
                            "start": 108000,
                            "end": 108060,
                            "duration": 60,
                            "source_start": 0,
                            "source_end": 60,
                            "source_fps": 30.0,
                            "media_pool_item_id": "mpi-1",
                            "media_pool_item_name": "parity-src-001",
                            "file_path": "media/parity-src-001.mp4",
                        }
                    ],
                }
            ],
        },
        "subtitle": {"track_count": 0, "tracks": []},
    },
    "markers": {},
}


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: dict[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _text_result(request_id: object, text: str) -> None:
    _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})


def _error(request_id: object, message: str, code: str) -> None:
    envelope = {
        "error": {
            "message": message,
            "code": code,
            "category": "resolve_api_failed",
            "retryable": False,
        }
    }
    _text_result(request_id, json.dumps(envelope))


_TRANSFORM_PAYLOAD = {
    "Pan": 0.0,
    "Tilt": 0.0,
    "ZoomX": 1.2,
    "ZoomY": 1.2,
    "RotationAngle": 0.0,
    "ZoomGang": True,
    "AnchorPointX": 0.5,
    "AnchorPointY": 0.5,
    "Pitch": 0.0,
    "Yaw": 0.0,
    "FlipX": False,
    "FlipY": False,
}

CANNED_ACTIONS: dict[tuple[str, str], dict[str, object]] = {
    ("resolve_control", "get_version"): VERSION_PAYLOAD,
    ("project_manager", "create"): {"success": True, "name": "Fake Project"},
    ("project_manager", "get_current"): {"success": True, "name": "Fake Project"},
    ("media_pool", "safe_import_media"): {
        "success": True,
        "imported": 2,
        "clips": [
            {"name": "parity-src-001", "id": "mpi-1"},
            {"name": "parity-src-002", "id": "mpi-2"},
        ],
    },
    ("media_pool", "create_timeline"): {
        "success": True,
        "name": "Fake Timeline",
        "id": "tl-fake-1",
        "created_new": True,
    },
    ("media_pool", "create_timeline_from_clips"): {
        "success": True,
        "name": "Fake Timeline",
        "id": "tl-fake-1",
        "created_new": True,
    },
    ("timeline", "probe_timeline_structure"): STRUCTURE_SNAPSHOT,
    ("timeline", "get_current"): {
        "name": "Fake Timeline",
        "id": "tl-fake-1",
        "start_frame": 108000,
        "end_frame": 108210,
        "start_timecode": "01:00:00:00",
    },
    ("timeline_item", "get_transform"): _TRANSFORM_PAYLOAD,
    ("render", "add_job"): {"job_id": "b5075b6c-53e1-44f2-be02-0377fb6a8ae7"},
    ("render", "get_job_status"): {"JobStatus": "Complete", "CompletionPercentage": 100},
    ("render", "get_format_and_codec"): {"format": "mp4", "codec": "h264"},
    ("render", "get_settings"): {
        "success": True,
        "FormatWidth": 1920,
        "FormatHeight": 1080,
        "FrameRate": 30.0,
        "AudioCodec": "aac",
        "AudioSampleRate": 48000,
    },
    ("render", "list_jobs"): {"jobs": [{"JobId": 7, "Status": "Complete"}]},
}


def _canned_action(tool: str, action: str) -> dict[str, object] | None:
    """Canned payload for one (tool, action); None lets the caller error out."""
    return CANNED_ACTIONS.get((tool, action))


def _handle_tools_call(request_id: object, payload: dict[str, object]) -> None:
    if TOOL_DELAY_SECONDS > 0:
        time.sleep(TOOL_DELAY_SECONDS)
    tool_name = payload.get("name")
    arguments = payload.get("arguments")
    if tool_name == "resolve_control" and isinstance(arguments, dict):
        action = str(arguments.get("action", ""))
        canned = _canned_action("resolve_control", action)
        if canned is not None:
            _text_result(request_id, json.dumps(canned))
            return
        _error(request_id, f"unknown action: {action}", "UNKNOWN_ACTION")
        return
    known_tools = {
        "project_manager", "media_pool", "timeline", "timeline_item", "render",
        "media_analysis", "edit_engine", "timeline_item_color",
    }
    if tool_name in known_tools and isinstance(arguments, dict):
        action = str(arguments.get("action", ""))
        canned = _canned_action(str(tool_name), action)
        if canned is not None:
            _text_result(request_id, json.dumps(canned))
            return
        _error(request_id, f"unknown action: {action}", "UNKNOWN_ACTION")
        return
    if tool_name == "echo":
        _text_result(request_id, json.dumps(arguments))
        return
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"unknown tool: {tool_name}"},
        }
    )


def main() -> None:
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed: object = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        request_id = parsed.get("id")
        if request_id is None:  # JSON-RPC notification: never answer
            continue
        method = parsed.get("method")
        if method == "initialize":
            _result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        elif method == "tools/list":
            _result(request_id, {"tools": CANNED_TOOLS})
        elif method == "tools/call":
            params = parsed.get("params")
            _handle_tools_call(request_id, params if isinstance(params, dict) else {})


if __name__ == "__main__":
    main()
