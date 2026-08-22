"""Server identity verification against the pinned deployment expectation.

After ``initialize`` the client compares the server's reported identity
(``serverInfo``) with the pinned expectation, refusing the connection with
:class:`McpVersionDriftError` BEFORE any tool call.

Two distinct version surfaces (live-verified against the pinned deployment):

- ``serverInfo.version`` is the ``mcp`` library version frozen in the pinned
  venv (``1.29.0``) — FastMCP advertises the library version, NOT the
  provider's own VERSION constant. This is what the handshake can check.
- the provider version ``2.98.3`` is the vendored ``src/server.py``
  ``VERSION`` constant (the capability fit matrix's ``provider_version``);
  it is carried as :data:`PINNED_PROVIDER_VERSION` for downstream execution
  call artifacts (task 8+), not for the handshake check.
"""

from __future__ import annotations

from typing import Final

from pydantic import ConfigDict, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.mcp_client.errors import McpClientError
from services.toolchain.mcp_fit import EXPECTED_PROVIDER_VERSION


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


PINNED_SERVER_NAME: Final = "DaVinciResolveMCP"
PINNED_HANDSHAKE_VERSION: Final = "1.29.0"
PINNED_PROVIDER_VERSION: Final = EXPECTED_PROVIDER_VERSION
PINNED_SERVER_IDENTITY: Final[ServerIdentity] = ServerIdentity(
    name=PINNED_SERVER_NAME, version=PINNED_HANDSHAKE_VERSION
)


class McpVersionDriftError(McpClientError):
    """Server identity does not match the pin; the connection is refused."""

    def __init__(
        self,
        reason: str,
        *,
        expected: ServerIdentity,
        reported: ServerIdentity | None,
    ) -> None:
        reported_text = (
            f"{reported.name} {reported.version}" if reported is not None else "<unparsable>"
        )
        super().__init__(
            f"server identity drift ({reason}): "
            f"expected {expected.name} {expected.version}, reported {reported_text}"
        )
        self.reason = reason
        self.expected = expected
        self.reported = reported


def verify_server_identity(
    handshake_payload: object, expected: ServerIdentity
) -> ServerHandshake:
    """Parse an initialize result; refuse drift before any tool call."""
    try:
        handshake = ServerHandshake.model_validate(handshake_payload)
    except ValidationError as exc:
        raise McpVersionDriftError(
            "unparsable-server-info", expected=expected, reported=None
        ) from exc
    if (
        handshake.server_info.name != expected.name
        or handshake.server_info.version != expected.version
    ):
        raise McpVersionDriftError(
            "identity-mismatch", expected=expected, reported=handshake.server_info
        )
    return handshake


__all__ = [
    "PINNED_HANDSHAKE_VERSION",
    "PINNED_PROVIDER_VERSION",
    "PINNED_SERVER_IDENTITY",
    "PINNED_SERVER_NAME",
    "McpVersionDriftError",
    "ServerHandshake",
    "ServerIdentity",
    "verify_server_identity",
]
