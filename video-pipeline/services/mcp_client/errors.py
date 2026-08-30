"""Typed errors for the MCP client package.

Every error raised by ``services.mcp_client`` is a :class:`McpClientError`
subclass, so callers can catch the package boundary with one type.
"""

from __future__ import annotations

from collections.abc import Mapping


class McpClientError(Exception):
    """Base class for every typed error raised by services.mcp_client."""


class McpSurfaceConfigError(McpClientError):
    """The committed MCP surface artifacts are missing, malformed, or stale.

    Task 11 fail-closed configuration failure: the inventory, dispositions,
    and manifest must load and agree in the same change set (a pin upgrade
    regenerates all three together). ``code``/``detail`` carry only safe
    identifiers — never absolute paths, raw schemas, or environment values.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class McpToolSurfaceDriftError(McpClientError):
    """The installed MCP surface differs from the committed inventory.

    ``findings`` maps a drift category (``tool-added``/``tool-removed``/
    ``schema-changed``/``action-added``/``action-removed``/
    ``provider-version``/``checkout-head``) to the sorted safe identifiers
    of that category — tool/action IDs, schema hashes, commits. Never raw
    schemas, secrets, environment values, media data, or absolute paths.
    """

    def __init__(
        self,
        findings: Mapping[str, tuple[str, ...]],
        *,
        pin_commit: str,
        inventory_sha256: str,
    ) -> None:
        parts = [
            f"{category}=[{', '.join(identifiers)}]"
            for category, identifiers in sorted(findings.items())
        ]
        parts.append(f"pin={pin_commit}")
        parts.append(f"inventory={inventory_sha256}")
        super().__init__("mcp-tool-surface-drift: " + "; ".join(parts))
        self.code = "mcp-tool-surface-drift"
        self.findings = {
            category: tuple(identifiers) for category, identifiers in findings.items()
        }
        self.pin_commit = pin_commit
        self.inventory_sha256 = inventory_sha256


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
    "McpSurfaceConfigError",
    "McpTimeoutError",
    "McpToolSurfaceDriftError",
    "McpTransportError",
]
