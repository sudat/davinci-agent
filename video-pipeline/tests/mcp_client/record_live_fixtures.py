"""Live fixture recorder for MCP contract fixtures (task 9/11).

Marked ``mcp_live``: the recording test runs ONLY when pytest selects
``-m mcp_live`` (task 11, operator environment with Resolve running).
Normal runs skip with an explicit reason.  When selected, the recorder
spawns the pinned server via the task-7 transport, calls the real tools,
and overwrites the synthetic seed fixtures plus ``.recording-meta.json``
(tool, params, recorded_at, server identity per fixture).

The ``media_analysis`` params are read from ``MCP_RECORD_STANDARD_PARAMS``
and ``MCP_RECORD_DEEP_PARAMS`` (JSON) so the operator can target the live
session's clip; the defaults analyze the currently selected clip.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from services.mcp_client.client import McpClient
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig
from services.toolchain.mcp_pin import load_mcp_pin

FIXTURES_DIR: Final = Path(__file__).resolve().parent / "fixtures"
VIDEO_PIPELINE_ROOT: Final = Path(__file__).resolve().parents[2]
PIN_PATH: Final = VIDEO_PIPELINE_ROOT / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
RECORDING_META: Final = ".recording-meta.json"

PLANNED_FIXTURES: Final = frozenset(
    {
        "server-info.json",
        "tools-list.json",
        "resolve-version.json",
        "media-analysis-standard.json",
        "deep-shot-analysis.json",
    }
)

DEFAULT_STANDARD_PARAMS: Final = '{"action": "analyze_clip", "params": {"selected": true}}'
DEFAULT_DEEP_PARAMS: Final = '{"action": "deepen", "params": {"selected": true}}'


def canonical_fixture_bytes(payload: object) -> bytes:
    """Canonical single-line JSON bytes (sort_keys, compact separators)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n"


def _call_tool_text(client: McpClient, name: str, arguments: Mapping[str, object]) -> str:
    """One raw tools/call round-trip; returns the first text content block."""
    response = client.transport.request(
        "tools/call", {"name": name, "arguments": dict(arguments)}
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise TypeError(f"tool {name!r} returned a non-object result")
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError(f"tool {name!r} returned no content blocks")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise RuntimeError(f"tool {name!r} returned a non-text content block")
    text = first.get("text")
    if not isinstance(text, str):
        raise TypeError(f"tool {name!r} returned a non-string text block")
    if result.get("isError"):
        raise RuntimeError(f"tool {name!r} reported an error: {text}")
    return text


def _record_all() -> None:
    """Spawn the pinned server, call the real tools, overwrite the fixtures."""
    pin = load_mcp_pin(PIN_PATH)
    config = StdioTransportConfig.from_pin(pin)
    client = McpClient(StdioJsonRpcTransport(config))
    try:
        client.connect()
        identity = client.get_server_info()
        server_identity = identity.model_dump(mode="json")
        recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        fixtures: dict[str, object] = {}
        meta: dict[str, object] = {}

        fixtures["server-info.json"] = server_identity
        meta["server-info.json"] = {
            "tool": "initialize",
            "params": {},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        tools_payload: dict[str, object] = {
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in client.list_tools()]
        }
        fixtures["tools-list.json"] = tools_payload
        meta["tools-list.json"] = {
            "tool": "tools/list",
            "params": {},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        version_text = _call_tool_text(client, "resolve_control", {"action": "get_version"})
        fixtures["resolve-version.json"] = json.loads(version_text)
        meta["resolve-version.json"] = {
            "tool": "resolve_control",
            "params": {"action": "get_version"},
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        standard_params = json.loads(
            os.environ.get("MCP_RECORD_STANDARD_PARAMS", DEFAULT_STANDARD_PARAMS)
        )
        standard_text = _call_tool_text(client, "media_analysis", standard_params)
        fixtures["media-analysis-standard.json"] = json.loads(standard_text)
        meta["media-analysis-standard.json"] = {
            "tool": "media_analysis",
            "params": standard_params,
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        deep_params = json.loads(os.environ.get("MCP_RECORD_DEEP_PARAMS", DEFAULT_DEEP_PARAMS))
        deep_text = _call_tool_text(client, "media_analysis", deep_params)
        fixtures["deep-shot-analysis.json"] = json.loads(deep_text)
        meta["deep-shot-analysis.json"] = {
            "tool": "media_analysis",
            "params": deep_params,
            "recorded_at": recorded_at,
            "server_identity": server_identity,
        }

        for name, payload in fixtures.items():
            (FIXTURES_DIR / name).write_bytes(canonical_fixture_bytes(payload))
        (FIXTURES_DIR / RECORDING_META).write_bytes(canonical_fixture_bytes(meta))
    finally:
        client.close()


@pytest.mark.mcp_live
def test_record_live_fixtures(pytestconfig: pytest.Config) -> None:
    """Record live tool responses over the seed fixtures (task 11)."""
    markexpr = pytestconfig.getoption("markexpr") or ""
    if "mcp_live" not in markexpr:
        pytest.skip("live recording requires -m mcp_live")
    _record_all()
