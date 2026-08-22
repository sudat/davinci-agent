"""Typed application client over the pinned MCP server.

The public surface is deliberately TYPED-ONLY: every method parses the wire
payload into a StrictModel and returns that model — no raw dict
pass-through, no free-form LLM tool-calling API (implementation plan 5.1).
The surface GROWS by adding one typed method per application need on top of
the private :meth:`McpClient._call_tool` base pattern; only the methods the
probe matrix needs today are implemented (server info, tools/list,
``resolve_control get_version``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Self

from pydantic import ConfigDict, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.mcp_client.errors import McpClientError
from services.mcp_client.transport import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    StdioJsonRpcTransport,
    StdioTransportConfig,
)
from services.mcp_client.version_pin import (
    PINNED_SERVER_IDENTITY,
    ServerIdentity,
    verify_server_identity,
)
from services.toolchain.mcp_pin import load_mcp_pin

PROTOCOL_VERSION: Final = "2024-11-05"
CLIENT_NAME: Final = "video-pipeline-mcp-client"
CLIENT_VERSION: Final = "mcp-client-v1"
RESOLVE_CONTROL_TOOL: Final = "resolve_control"


class McpToolCallError(McpClientError):
    """A tool call failed: unparsable result envelope or server-side error."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"tool {tool_name!r} failed: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class McpToolInfo(StrictModel):
    """One ``tools/list`` entry; unknown protocol keys are ignored."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    name: str
    description: str | None = None
    input_schema: dict[str, object] = Field(alias="inputSchema")


class _ToolContentBlock(StrictModel):
    """Text content block of a tools/call result envelope."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    type: str
    text: str


class _ToolCallResult(StrictModel):
    """tools/call result envelope; unknown protocol keys are ignored."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    content: list[_ToolContentBlock]
    is_error: bool = Field(alias="isError", default=False)


class _ToolsListEnvelope(StrictModel):
    """tools/list result envelope; unknown protocol keys are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    tools: list[McpToolInfo]


class McpToolResult(StrictModel):
    """Application-facing tool result: first text block plus the error flag."""

    text: str
    is_error: bool


class ResolveVersionReport(StrictModel):
    """Seed shape of ``resolve_control {action: get_version}`` text payload.

    Synthetic seed contract for the fake server; task 9/11 replace it with
    the recorded live fixture contract before Gate V43-0 acceptance.
    """

    connected: bool
    version: str


class McpClient:
    """Typed, single-writer client; one client owns one server process."""

    def __init__(
        self,
        transport: StdioJsonRpcTransport,
        *,
        expected_identity: ServerIdentity = PINNED_SERVER_IDENTITY,
    ) -> None:
        self._transport = transport
        self._expected_identity = expected_identity
        self._server_info: ServerIdentity | None = None

    @classmethod
    def from_pin(
        cls,
        pin_path: Path,
        *,
        clone_dir: Path | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> McpClient:
        """Build a client from the task-2 pin contract file."""
        pin = load_mcp_pin(pin_path)
        config = StdioTransportConfig.from_pin(
            pin, clone_dir=clone_dir, request_timeout_seconds=request_timeout_seconds
        )
        return cls(StdioJsonRpcTransport(config))

    @property
    def transport(self) -> StdioJsonRpcTransport:
        return self._transport

    def connect(self) -> ServerIdentity:
        """Start the server, initialize, verify identity; refuse on drift."""
        if self._server_info is not None:
            return self._server_info
        self._transport.start()
        try:
            response = self._transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            )
            handshake = verify_server_identity(
                response.get("result"), self._expected_identity
            )
            self._transport.send_notification("notifications/initialized")
        except McpClientError:
            self._transport.close()
            raise
        self._server_info = handshake.server_info
        return self._server_info

    def get_server_info(self) -> ServerIdentity:
        """The verified server identity from ``connect`` (typed)."""
        if self._server_info is None:
            raise McpClientError("client is not connected; call connect() first")
        return self._server_info

    def list_tools(self) -> tuple[McpToolInfo, ...]:
        """Discover the server's tool metadata via ``tools/list``."""
        response = self._transport.request("tools/list")
        try:
            envelope = _ToolsListEnvelope.model_validate(response.get("result"))
        except ValidationError as exc:
            raise McpClientError(f"unparsable tools/list result: {exc}") from exc
        return tuple(envelope.tools)

    def resolve_get_version(self) -> ResolveVersionReport:
        """``resolve_control {action: get_version}`` as a typed report."""
        result = self._call_tool(RESOLVE_CONTROL_TOOL, {"action": "get_version"})
        try:
            payload: object = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise McpToolCallError(
                RESOLVE_CONTROL_TOOL, "version payload is not JSON"
            ) from exc
        try:
            return ResolveVersionReport.model_validate(payload)
        except ValidationError as exc:
            raise McpToolCallError(
                RESOLVE_CONTROL_TOOL, "version payload shape mismatch"
            ) from exc

    def close(self) -> None:
        """Stop the server subprocess group (idempotent)."""
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _call_tool(self, name: str, arguments: Mapping[str, object]) -> McpToolResult:
        """Typed-method base: one tools/call round-trip, envelope parsed."""
        response = self._transport.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        try:
            envelope = _ToolCallResult.model_validate(response.get("result"))
        except ValidationError as exc:
            raise McpToolCallError(name, "unparsable tool result envelope") from exc
        text = envelope.content[0].text if envelope.content else ""
        if envelope.is_error:
            raise McpToolCallError(name, f"server reported error: {text}")
        return McpToolResult(text=text, is_error=envelope.is_error)


__all__ = [
    "McpClient",
    "McpToolCallError",
    "McpToolInfo",
    "McpToolResult",
    "ResolveVersionReport",
]
