"""Read-only surface discovery client for inventory generation (Task 11).

The Task 9 inventory generator must inspect a possibly CHANGED pin before
the new inventory exists — so this client is deliberately NOT surface-gated
and deliberately CANNOT mutate anything: its entire exposed surface is
``initialize`` handshake validation, ``tools/list`` capture, and ``close``.
There is no tool-call seam of any kind on this class.

Pin-backed PRODUCT connections use
:class:`services.mcp_client.client.McpClient`, which always applies the
committed surface gate. This module is the explicitly named, read-only
exception — the inventory measuring instrument.
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
from services.mcp_client.version_pin import (
    PINNED_SERVER_IDENTITY,
    ServerIdentity,
    verify_server_identity,
)
from services.toolchain.mcp_pin import load_mcp_pin

PROTOCOL_VERSION: Final = "2024-11-05"
DISCOVERY_CLIENT_NAME: Final = "video-pipeline-mcp-discovery"
DISCOVERY_CLIENT_VERSION: Final = "mcp-discovery-v1"


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
    """Pin-backed discovery: initialize + tools/list + close, nothing else."""

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
    ) -> McpDiscoveryClient:
        """Spawn spec from the pin file — ungated BY DESIGN (see module doc)."""
        pin = load_mcp_pin(pin_path)
        venv_python = Path(pin.venv_python)
        config = StdioTransportConfig(
            command=(pin.venv_python, pin.server_entry_point),
            cwd=clone_dir if clone_dir is not None else venv_python.parent.parent.parent,
            extra_env={"DAVINCI_RESOLVE_MCP_UPDATE_CHECK": "0"},
            request_timeout_seconds=request_timeout_seconds,
        )
        return cls(StdioJsonRpcTransport(config))

    def connect(self) -> ServerIdentity:
        """Start the server, initialize, verify identity (no gate: discovery)."""
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
            handshake = verify_server_identity(
                response.get("result"), self._expected_identity
            )
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


__all__ = ["McpDiscoveryClient", "McpToolInfo", "ToolsListEnvelope"]
