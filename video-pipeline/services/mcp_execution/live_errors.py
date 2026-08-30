"""Typed errors shared across the live execution seam.

Neutral home for the adapter/handler error types so media-boundary
modules can raise the same typed failures without importing the handler
package (the CLI-facing reconciliation boundary imports from here).
"""

from __future__ import annotations

from services.mcp_client.errors import McpClientError


class LiveAdapterError(McpClientError):
    def __init__(self, code: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        #: Typed retryability signal for the execution runner: the default
        #: keeps a failure inside the step's bounded transient-retry budget;
        #: ``retryable=False`` marks a DETERMINISTIC failure (a measured
        #: policy verdict on an already-mutated timeline) whose retry can
        #: only re-measure the mutated state and overwrite first-attempt
        #: evidence. The runner reads this flag — never message strings.
        self.retryable = retryable


class LiveAdapterUnsupportedError(LiveAdapterError):
    pass


__all__ = ["LiveAdapterError", "LiveAdapterUnsupportedError"]
