"""Read-only surface discovery client for the vendored MCP server.

Its entire exposed surface is ``initialize`` handshake validation,
``tools/list`` capture, and ``close``. There is no tool-call seam of any
kind on this class, so it cannot mutate anything.
"""

from __future__ import annotations

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

PROTOCOL_VERSION: Final = "2024-11-05"
DISCOVERY_CLIENT_NAME: Final = "video-pipeline-mcp-discovery"
DISCOVERY_CLIENT_VERSION: Final = "mcp-discovery-v1"
#: The only identity the client enforces: the handshake server name must be
#: the vendored Resolve MCP server. The reported version is surfaced to
#: callers, never refused on.
EXPECTED_SERVER_NAME: Final = "DaVinciResolveMCP"


class ServerIdentity(StrictModel):
    """The ``serverInfo`` object of an MCP initialize response."""

    name: str
    version: str


class ServerHandshake(StrictModel):
    """The initialize result envelope; unknown protocol keys are ignored."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    protocol_version: str = Field(alias="protocolVersion")
    server_info: ServerIdentity = Field(alias="serverInfo")


class McpServerNameError(McpClientError):
    """The spawned server is not the vendored Resolve MCP server; refused."""

    def __init__(
        self,
        *,
        expected: str,
        reported: ServerIdentity | None,
    ) -> None:
        reported_text = (
            f"{reported.name} {reported.version}" if reported is not None else "<unparsable>"
        )
        super().__init__(
            f"server identity mismatch: expected {expected}, reported {reported_text}"
        )
        self.expected = expected
        self.reported = reported


def verify_server_name(handshake_payload: object) -> ServerHandshake:
    """Parse an initialize result; refuse unless the server name matches.

    The reported version is carried on the handshake for callers to report;
    it is never a refusal reason.
    """
    try:
        handshake = ServerHandshake.model_validate(handshake_payload)
    except ValidationError as exc:
        raise McpServerNameError(
            expected=EXPECTED_SERVER_NAME, reported=None
        ) from exc
    if handshake.server_info.name != EXPECTED_SERVER_NAME:
        raise McpServerNameError(
            expected=EXPECTED_SERVER_NAME, reported=handshake.server_info
        )
    return handshake


class McpToolInfo(StrictModel):
    """One ``tools/list`` entry; unknown protocol keys are ignored."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    name: str
    description: str | None = None
    input_schema: dict[str, object] = Field(alias="inputSchema")


class ToolsListEnvelope(StrictModel):
    """tools/list result envelope; unknown protocol keys are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    tools: list[McpToolInfo]


class McpDiscoveryClient:
    """Discovery over the vendored server: initialize + tools/list + close."""

    def __init__(
        self,
        transport: StdioJsonRpcTransport,
    ) -> None:
        self._transport = transport
        self._server_info: ServerIdentity | None = None

    @classmethod
    def from_defaults(
        cls,
        *,
        vendor_dir: Path | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> McpDiscoveryClient:
        """Spawn spec for the vendored checkout (explicit dir for tests)."""
        config = StdioTransportConfig.from_defaults(
            vendor_dir=vendor_dir,
            request_timeout_seconds=request_timeout_seconds,
        )
        return cls(StdioJsonRpcTransport(config))

    def connect(self) -> ServerIdentity:
        """Start the server, initialize, verify the server name."""
        if self._server_info is not None:
            return self._server_info
        self._transport.start()
        try:
            response = self._transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": DISCOVERY_CLIENT_NAME,
                        "version": DISCOVERY_CLIENT_VERSION,
                    },
                },
            )
            handshake = verify_server_name(response.get("result"))
            self._transport.send_notification("notifications/initialized")
        except McpClientError:
            self._transport.close()
            raise
        self._server_info = handshake.server_info
        return self._server_info

    def list_tools(self) -> tuple[McpToolInfo, ...]:
        """Discover the server's tool metadata via ``tools/list``."""
        response = self._transport.request("tools/list")
        try:
            envelope = ToolsListEnvelope.model_validate(response.get("result"))
        except ValidationError as exc:
            raise McpClientError(f"unparsable tools/list result: {exc}") from exc
        return tuple(envelope.tools)

    def close(self) -> None:
        """Stop the server subprocess group (idempotent)."""
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


__all__ = [
    "EXPECTED_SERVER_NAME",
    "McpDiscoveryClient",
    "McpServerNameError",
    "McpToolInfo",
    "ServerHandshake",
    "ServerIdentity",
    "ToolsListEnvelope",
    "verify_server_name",
]
