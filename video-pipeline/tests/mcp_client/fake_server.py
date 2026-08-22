"""Fake stdio MCP server for ``services.mcp_client`` unit tests.

A minimal JSON-RPC 2.0 responder intended ONLY as a test subprocess: it
answers ``initialize`` / ``tools/list`` / ``tools/call`` from canned
results, one LF-delimited JSON object per line. Test modes via environment
variables (read once at spawn):

- ``FAKE_MCP_SERVER_NAME`` / ``FAKE_MCP_SERVER_VERSION`` override the
  reported ``serverInfo`` — set a foreign name for identity-mismatch mode.
- ``FAKE_MCP_DELAY_SECONDS`` sleeps before answering ``tools/call`` —
  set it far above any client timeout for the hung-server mode.
"""

from __future__ import annotations

import json
import os
import sys
import time

SERVER_NAME = os.environ.get("FAKE_MCP_SERVER_NAME", "DaVinciResolveMCP")
SERVER_VERSION = os.environ.get("FAKE_MCP_SERVER_VERSION", "1.29.0")
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


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: dict[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _text_result(request_id: object, text: str) -> None:
    _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})


def _handle_tools_call(request_id: object, payload: dict[str, object]) -> None:
    if TOOL_DELAY_SECONDS > 0:
        time.sleep(TOOL_DELAY_SECONDS)
    tool_name = payload.get("name")
    if tool_name == "resolve_control":
        _text_result(request_id, json.dumps({"connected": True, "version": "21.0.4.5"}))
    elif tool_name == "echo":
        _text_result(request_id, json.dumps(payload.get("arguments")))
    else:
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
