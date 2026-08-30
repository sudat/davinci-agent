"""The only public product mutation adapter over the pinned Resolve MCP.

Classification and dispatch for the live execution seam: logical product
surfaces are checked against SUPPORTED_SURFACES (the single support
authority) and routed to the internal domain handlers in ``live_handlers``
that share one typed session context. Everything else fails closed, typed,
before the raw transport is touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, get_args

from services.mcp_client.execution_runner import McpTransportFn  # noqa: TC001
from services.mcp_execution.live_handlers import HANDLERS
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveSessionContext,
)
from services.mcp_execution.plan_models import ToolSurface

if TYPE_CHECKING:
    from services.mcp_execution.audio_measurement import AudioMeasureFn
    from services.mcp_execution.color_measurement import FrameDiffFn

#: The single source of truth for which product surfaces this adapter
#: dispatches to the pinned MCP. Every other surface is refused typed,
#: before the raw transport is touched.
SUPPORTED_SURFACES: Final[frozenset[ToolSurface]] = frozenset(
    (
        "prepare_project",
        "safe_import_media",
        "append_to_timeline",
        "set_voice_isolation_state",
        "subtitle_generation_probe",
        "safe_set_audio_properties",
        "render_boundary_report",
        "safe_apply_drx",
        "render_native",
        "set_transform",
    )
)

#: Derived from the ToolSurface vocabulary itself: membership here separates
#: "known product surface, not live yet" from "unknown surface name".
_KNOWN_SURFACES: Final[frozenset[str]] = frozenset(get_args(ToolSurface))


class LiveMcpAdapter:
    def __init__(  # noqa: PLR0913 (keyword surface mirrors the adapter's injected ports)
        self,
        transport: McpTransportFn,
        *,
        media_paths: Mapping[str, str] | None = None,
        audio_measure: AudioMeasureFn | None = None,
        render_dir: str | None = None,
        frame_diff: FrameDiffFn | None = None,
        media_frame_counts: Mapping[str, int] | None = None,
    ) -> None:
        self._ctx = LiveSessionContext.build(
            transport,
            media_paths,
            audio_measure=audio_measure,
            render_dir=render_dir,
            frame_diff=frame_diff,
            media_frame_counts=media_frame_counts,
        )

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,  # noqa: ARG002 (deadline authority is handler-side)
    ) -> object:
        # Classification precedes params validation on purpose: known-but-
        # unimplemented surfaces must fail closed even with valid params.
        # ``timeout_seconds`` satisfies the McpTransportFn protocol shape;
        # per-operation deadlines are applied by the handlers against the
        # RAW transport (ctx.transport), never at the adapter surface.
        if tool_name not in SUPPORTED_SURFACES:
            if tool_name in _KNOWN_SURFACES:
                raise LiveAdapterUnsupportedError("surface-not-live", f"{tool_name} has no live MCP handler")  # noqa: E501
            raise LiveAdapterUnsupportedError("surface-unknown", f"{tool_name!r} is not a product surface")  # noqa: E501
        handler = HANDLERS.get(tool_name)
        if handler is None:
            raise LiveAdapterError("surface-handler-missing", f"{tool_name!r} is supported but unrouted")  # noqa: E501
        return handler(self._ctx, action, normalized_params)

    @property
    def timeline_mutated(self) -> bool:
        """Whether any handler mutated timeline content this session."""

        return self._ctx.timeline_mutated


# Error types are owned by live_handlers.common (handlers raise them); this
# module stays the public import surface product code and tests already use.
__all__ = ["SUPPORTED_SURFACES", "LiveAdapterError", "LiveAdapterUnsupportedError", "LiveMcpAdapter"]  # noqa: E501
