"""Vendored stdio MCP client with a typed application surface."""

from __future__ import annotations

from services.mcp_client.client import (
    McpClient,
    McpToolCallError,
    McpToolInfo,
    McpToolResult,
    ResolveVersionPayload,
)
from services.mcp_client.discovery import (
    EXPECTED_SERVER_NAME,
    McpServerNameError,
    ServerHandshake,
    ServerIdentity,
    verify_server_name,
)
from services.mcp_client.errors import McpClientError
from services.mcp_client.transport import (
    McpJsonRpcError,
    McpTimeoutError,
    McpTransportError,
    StdioJsonRpcTransport,
    StdioTransportConfig,
)

__all__ = [
    "EXPECTED_SERVER_NAME",
    "McpClient",
    "McpClientError",
    "McpJsonRpcError",
    "McpServerNameError",
    "McpTimeoutError",
    "McpToolCallError",
    "McpToolInfo",
    "McpToolResult",
    "McpTransportError",
    "ResolveVersionPayload",
    "ServerHandshake",
    "ServerIdentity",
    "StdioJsonRpcTransport",
    "StdioTransportConfig",
    "verify_server_name",
]
