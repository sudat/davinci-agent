"""Typed errors for the MCP client package.

Every error raised by ``services.mcp_client`` is a :class:`McpClientError`
subclass, so callers can catch the package boundary with one type.
"""

from __future__ import annotations


class McpClientError(Exception):
    """Base class for every typed error raised by services.mcp_client."""


class McpTransportError(McpClientError):
    """Server process is gone, was never started, or broke the wire format."""


class McpTimeoutError(McpClientError):
    """No response arrived inside the explicit per-request timeout."""

    def __init__(self, method: str, timeout_seconds: float) -> None:
        super().__init__(f"no response for {method!r} within {timeout_seconds}s")
        self.method = method
        self.timeout_seconds = timeout_seconds


class McpJsonRpcError(McpClientError):
    """The server answered this request with a JSON-RPC error object."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method!r} failed with code {code}: {message}")
        self.method = method
        self.code = code
        self.rpc_message = message


__all__ = [
    "McpClientError",
    "McpJsonRpcError",
    "McpTimeoutError",
    "McpTransportError",
]
