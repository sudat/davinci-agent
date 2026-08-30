"""Typed application client over the pinned MCP server.

The public surface is deliberately TYPED-ONLY: every method parses the wire
payload into a StrictModel and returns that model — no raw dict
pass-through, no free-form LLM tool-calling API (implementation plan 5.1).
The surface GROWS by adding one typed method per application need on top of
the private :meth:`McpClient._call_tool` base pattern; only the methods the
probe matrix needs today are implemented (server info, tools/list,
``resolve_control get_version``).

Task 11 — mandatory committed-surface validation: a client whose transport
config is pin-backed (built via :meth:`StdioTransportConfig.from_pin` or
:meth:`McpClient.from_pin`) loads the committed inventory/dispositions/
manifest baseline AT CONSTRUCTION and validates the installed surface
during :meth:`connect`. There is no opt-out: manual (non-pin) configs are
the test/fake path, and read-only pin inspection without a committed
inventory uses :class:`services.mcp_client.discovery.McpDiscoveryClient`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

from pydantic import ConfigDict, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.mcp_client.discovery import McpToolInfo, ToolsListEnvelope
from services.mcp_client.errors import McpClientError
from services.mcp_client.response_normalize import (
    ResolveVersionPayload,
    normalize_resolve_version,
)
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
from services.toolchain.mcp_coverage_models import LiveTool
from services.toolchain.mcp_pin import load_mcp_pin

if TYPE_CHECKING:
    # Import-cycle break: mcp_surface_gate imports services.mcp_client.errors,
    # which initializes this package's __init__ and imports client back. The
    # gate loader is therefore imported at RUNTIME inside _gate_for only.
    from services.toolchain.mcp_surface_gate import CommittedSurface

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


class McpToolResult(StrictModel):
    """Application-facing tool result: first text block plus the error flag."""

    text: str
    is_error: bool


def _gate_tools(tools: tuple[McpToolInfo, ...]) -> tuple[LiveTool, ...]:
    """Convert ``tools/list`` entries to the Task 9 gate model (name + schema)."""
    return tuple(LiveTool(name=tool.name, input_schema=tool.input_schema) for tool in tools)


class McpClient:
    """Typed, single-writer client; one client owns one server process.

    Pin-backed constructions (config built via ``StdioTransportConfig.from_pin``)
    load the committed surface gate eagerly at construction; manual test
    constructions carry no pin provenance and stay ungated.
    """

    def __init__(
        self,
        transport: StdioJsonRpcTransport,
        *,
        expected_identity: ServerIdentity = PINNED_SERVER_IDENTITY,
    ) -> None:
        self._transport = transport
        self._expected_identity = expected_identity
        self._server_info: ServerIdentity | None = None
        self._surface_gate = self._gate_for(transport)

    @staticmethod
    def _gate_for(transport: StdioJsonRpcTransport) -> CommittedSurface | None:
        # Runtime import: hoisting this to module level reintroduces the
        # startup ImportError documented in the TYPE_CHECKING note above.
        from services.toolchain.mcp_surface_gate import (  # noqa: PLC0415 (cycle break)
            load_committed_surface,
        )

        context = transport.config.surface
        if context is None:
            return None
        return load_committed_surface(
            pin=context.pin,
            clone_dir=context.clone_dir,
            coverage_dir=context.coverage_dir,
        )

    @classmethod
    def from_pin(
        cls,
        pin_path: Path,
        *,
        clone_dir: Path | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        coverage_dir: Path | None = None,
    ) -> McpClient:
        """Build a gated client from the task-2 pin contract file.

        The committed-surface gate always applies: ``coverage_dir`` only
        selects WHICH committed artifact tree validates the connection
        (tests inject a fixture tree); there is no validation opt-out.
        """
        pin = load_mcp_pin(pin_path)
        config = StdioTransportConfig.from_pin(
            pin,
            clone_dir=clone_dir,
            request_timeout_seconds=request_timeout_seconds,
            coverage_dir=coverage_dir,
        )
        return cls(StdioJsonRpcTransport(config))

    @property
    def transport(self) -> StdioJsonRpcTransport:
        return self._transport

    def connect(self) -> ServerIdentity:
        """Start the server, initialize, verify identity; refuse on drift.

        Ordering is load-bearing (Task 11): handshake validation, then the
        ``tools/list`` capture, then the committed-surface validation — only
        after all three does the client reach the connected/usable state.
        A gate failure closes the session, so no tool call can follow it.
        """
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
            if self._surface_gate is not None:
                self._surface_gate.validate_installed(_gate_tools(self.list_tools()))
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
            envelope = ToolsListEnvelope.model_validate(response.get("result"))
        except ValidationError as exc:
            raise McpClientError(f"unparsable tools/list result: {exc}") from exc
        return tuple(envelope.tools)

    def surface_gate(self) -> CommittedSurface | None:
        """The committed-surface gate this client enforces (None if manual)."""
        return self._surface_gate

    def resolve_get_version(self) -> ResolveVersionPayload:
        """``resolve_control {action: get_version}`` as a typed report (live shape)."""
        payload = self._call_action_json(RESOLVE_CONTROL_TOOL, "get_version", {})
        return normalize_resolve_version(payload)

    def close(self) -> None:
        """Stop the server subprocess group (idempotent)."""
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> McpToolResult:
        """Typed-method base: one tools/call round-trip, envelope parsed."""
        response = self._transport.request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout_seconds=timeout_seconds,
        )
        try:
            envelope = _ToolCallResult.model_validate(response.get("result"))
        except ValidationError as exc:
            raise McpToolCallError(name, "unparsable tool result envelope") from exc
        text = envelope.content[0].text if envelope.content else ""
        if envelope.is_error:
            raise McpToolCallError(name, f"server reported error: {text}")
        return McpToolResult(text=text, is_error=envelope.is_error)

    def _call_action_json(
        self,
        tool: str,
        action: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        """JSON parse seam for the typed ops surface (task 11).

        Returns the decoded ``{action, params}`` payload as a JSON object;
        callers (:mod:`services.mcp_client.ops`) immediately validate it into
        a frozen result model — this is a package-private seam, not public
        surface.
        """
        result = self._call_tool(
            tool, {"action": action, "params": dict(params)},
            timeout_seconds=timeout_seconds,
        )
        try:
            payload: object = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise McpToolCallError(tool, f"{action} payload is not JSON") from exc
        return payload


__all__ = [
    "McpClient",
    "McpToolCallError",
    "McpToolInfo",
    "McpToolResult",
    "ResolveVersionPayload",
]
