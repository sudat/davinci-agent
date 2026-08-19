"""Carry the styled presentation through the preview (Todo 57).

The preview stays a soft-sub ``mov_text`` render — styling is NEVER burned
into the pixels. What the preview carries is the correspondence guarantee
(every styled cue's text and record span must match the subtitle items the
SRT is generated from) plus the recorded style table in the trace manifest,
so editorial review sees exactly which presentation would be applied at
finalization.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.preview.models import PreviewLayout, PreviewLayoutError, TraceStyleTable

if TYPE_CHECKING:
    from services.presentation.styling_models import StyledPresentation


def carry_styled(layout: PreviewLayout, styled: StyledPresentation) -> TraceStyleTable:
    """Verify styled/IR subtitle correspondence and build the trace style table."""

    layout_cues = sorted(
        ((item.record_span, item.subtitle_text or "") for item in layout.subtitle_items),
        key=lambda entry: (entry[0].start_frame, entry[0].end_frame, entry[1]),
    )
    styled_cues = sorted(
        ((cue.record_span, cue.text) for cue in styled.cues),
        key=lambda entry: (entry[0].start_frame, entry[0].end_frame, entry[1]),
    )
    if layout_cues != styled_cues:
        raise PreviewLayoutError(
            "styled presentation cues do not match the preview subtitle items "
            "(text/record-span drift); restyling requires a recompiled IR"
        )
    return TraceStyleTable(
        style_id=styled.style_id,
        params=styled.style,
        cue_style_ids=tuple(cue.style_id for cue in styled.cues),
        titled_item_ids=tuple(item.item_id for item in styled.titled_items),
        styled_presentation_sha256=hashlib.sha256(
            canonical_model_bytes(styled)
        ).hexdigest(),
    )


__all__ = ["carry_styled"]
