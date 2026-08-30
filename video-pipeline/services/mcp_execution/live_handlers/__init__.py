"""Live handler registry dispatched by ``LiveMcpAdapter`` (Task 3 seam).

``LiveMcpAdapter`` (services.mcp_execution.live_adapter) remains the only
public product mutation adapter/dispatcher; these modules are internal
domain seams. Each handler consumes the shared ``LiveSessionContext`` and
follows validate → vendor operation → typed parse → readback → result.
Wiring a surface here must move it into the adapter's SUPPORTED_SURFACES
authority in the same change — the architecture tests pin the lockstep.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from services.mcp_execution.live_handlers import (
    audio,
    color,
    placement,
    render,
    subtitle,
    transform,
)
from services.mcp_execution.live_handlers.common import LiveSessionContext

#: A domain handler: (shared session context, action, normalized params).
HandlerFn = Callable[[LiveSessionContext, str, Mapping[str, object]], object]

#: Routed handlers keyed by product surface name; keys must equal the
#: adapter's SUPPORTED_SURFACES exactly.
HANDLERS: Final[Mapping[str, HandlerFn]] = {
    "prepare_project": placement.prepare_project,
    "safe_import_media": placement.safe_import_media,
    "append_to_timeline": placement.append_to_timeline,
    "set_voice_isolation_state": audio.set_voice_isolation_state,
    "subtitle_generation_probe": subtitle.apply_subtitles,
    "safe_set_audio_properties": audio.apply_dialogue_preset,
    "render_boundary_report": audio.measure_audio_stage,
    "safe_apply_drx": color.apply_drx_grade,
    "render_native": render.render_native,
    "set_transform": transform.apply_transform,
}

__all__ = ["HANDLERS", "HandlerFn"]
