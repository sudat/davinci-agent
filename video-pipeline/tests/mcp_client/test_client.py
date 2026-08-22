"""McpClient contract against the fake stdio MCP server subprocess.

Covers the task-7 acceptance surface: launch + initialize with typed server
info, tools/list discovery, typed tool-call round-trip, explicit per-request
timeout (bounded wall clock, no hang), identity drift refusal BEFORE any
tool call, and warning-free shutdown — ``filterwarnings = ["error"]`` turns
any unclosed pipe into a test error, so clean close is enforced implicitly.
"""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import pytest

from services.mcp_client.client import McpClient
from services.mcp_client.transport import (
    McpTimeoutError,
    McpTransportError,
    StdioJsonRpcTransport,
    StdioTransportConfig,
)
from services.mcp_client.version_pin import McpVersionDriftError

FAKE_SERVER_PATH = Path(__file__).resolve().parent / "fake_server.py"


def _client(
    *, request_timeout_seconds: float = 5.0, env: dict[str, str] | None = None
) -> McpClient:
    config = StdioTransportConfig(
        command=(sys.executable, str(FAKE_SERVER_PATH)),
        request_timeout_seconds=request_timeout_seconds,
        extra_env=env or {},
    )
    return McpClient(StdioJsonRpcTransport(config))


def test_launch_initialize_returns_typed_server_info() -> None:
    with _client() as client:
        identity = client.connect()
        assert identity.name == "DaVinciResolveMCP"
        assert identity.version == "1.29.0"
        assert client.get_server_info() == identity


def test_list_tools_discovers_canned_surface() -> None:
    with _client() as client:
        client.connect()
        tools = client.list_tools()
    names = {tool.name for tool in tools}
    assert names == {"resolve_control", "echo"}
    resolve_control = next(tool for tool in tools if tool.name == "resolve_control")
    assert resolve_control.input_schema["type"] == "object"


def test_resolve_get_version_round_trips_typed_payload() -> None:
    with _client() as client:
        client.connect()
        report = client.resolve_get_version()
    assert report.connected is True
    assert report.version == "21.0.4.5"


def test_slow_tool_times_out_with_typed_error_and_bounded_wall_clock() -> None:
    client = _client(request_timeout_seconds=0.5, env={"FAKE_MCP_DELAY_SECONDS": "30"})
    started = time.monotonic()
    with client:
        client.connect()
        with pytest.raises(McpTimeoutError) as excinfo:
            client.resolve_get_version()
        elapsed = time.monotonic() - started
    assert excinfo.value.method == "tools/call"
    assert excinfo.value.timeout_seconds == 0.5
    assert elapsed < 10.0


def test_identity_mismatch_refuses_connection_before_any_tool_call() -> None:
    client = _client(env={"FAKE_MCP_SERVER_NAME": "rogue-server"})
    with pytest.raises(McpVersionDriftError) as excinfo:
        client.connect()
    assert excinfo.value.reported is not None
    assert excinfo.value.reported.name == "rogue-server"
    assert excinfo.value.expected.name == "DaVinciResolveMCP"
    with pytest.raises(McpTransportError):
        client.resolve_get_version()


def test_context_manager_shutdown_is_clean_without_resource_warnings() -> None:
    client = _client()
    with client:
        client.connect()
        client.resolve_get_version()
    process = client.transport.process
    assert process is not None
    assert process.poll() is not None
    gc.collect()  # filterwarnings=error turns any unclosed pipe into a failure
