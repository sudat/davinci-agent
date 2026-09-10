"""Subtitle QC policy and transcript cue-source models (Todo 44).

The policy is the single declared source for subtitle limits (minimum display
duration, line count, characters per line) and the closed set of style ids —
cue generation may only reference declared styles (Phase 3 introduces real
styles; Phase 1 carries one fixed default ref).

Transcript cue sources are the DECLARED subtitle inputs: half-open spans on
the edit-source frame axis or on the edit-source audio sample axis. Floats
cannot be expressed anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, PositiveInteger, StrictModel
from services.outputs.geometry import subtitle_chars_for_output

if TYPE_CHECKING:
    from services.outputs.geometry import OutputId

CueSpan = Annotated[
    "FrameCueSpan | SampleCueSpan",
    Field(union_mode="smart"),
]


class FrameCueSpan(StrictModel):
    """Half-open cue span on the edit-source video frame axis."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameCueSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "span_empty", "cue spans are non-empty half-open ranges"
            )
        return self


class SampleCueSpan(StrictModel):
    """Half-open cue span on the edit-source audio sample axis."""

    start_sample: Frame
    end_sample: Frame
    sample_rate: PositiveInteger

    @model_validator(mode="after")
    def require_forward(self) -> SampleCueSpan:
        if self.end_sample <= self.start_sample:
            raise PydanticCustomError(
                "span_empty", "cue spans are non-empty half-open ranges"
            )
        return self


class TranscriptCueSegment(StrictModel):
    """One declared dialogue cue: text plus its source span."""

    segment_id: Identifier
    text: str = Field(min_length=1, strict=True)
    span: CueSpan


class TranscriptCueSource(StrictModel):
    """The declared transcript cue table for one episode (Japanese only)."""

    language: Literal["ja"]
    segments: tuple[TranscriptCueSegment, ...] = ()

    @model_validator(mode="after")
    def require_unique_segment_ids(self) -> TranscriptCueSource:
        ids = [segment.segment_id for segment in self.segments]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_segment", "transcript cue segment ids are unique"
            )
        return self


class SubtitleQcPolicy(StrictModel):
    """Frozen subtitle limits plus the closed declared style set."""

    policy_id: Identifier
    min_duration_frames: int = Field(gt=0, strict=True)
    max_lines: int = Field(gt=0, strict=True)
    max_chars_per_line: int = Field(gt=0, strict=True)
    declared_style_refs: tuple[Identifier, ...] = Field(min_length=1)
    default_style_ref: Identifier

    @model_validator(mode="after")
    def require_declared_default_style(self) -> SubtitleQcPolicy:
        if len(set(self.declared_style_refs)) != len(self.declared_style_refs):
            raise PydanticCustomError(
                "duplicate_style_ref", "declared style refs are unique"
            )
        if self.default_style_ref not in self.declared_style_refs:
            raise PydanticCustomError(
                "style_ref_undeclared",
                "default_style_ref {ref} must be a declared style id",
                {"ref": self.default_style_ref},
            )
        return self

    def for_output(self, output_id: OutputId) -> SubtitleQcPolicy:
        """The policy re-derived for one output canvas (landscape identical).

        The vertical canvas is narrower, so the cue line-wrap width scales
        by the canvas width ratio; all other limits are canvas-agnostic.
        """

        return self.model_copy(
            update={
                "max_chars_per_line": subtitle_chars_for_output(
                    self.max_chars_per_line, output_id
                )
            }
        )


# Presentation wrap rules (moved from
# services/episode_cockpit/presentation_overrides.py: the step constants +
# wrap derivation live beside this subtitle rule module; the journal
# derivation loop stays there and imports from here).

# The subtitle base width is the frozen Phase-1 QC policy
# (``services/cli/compile_ir.py:qc_policy`` ``max_chars_per_line=20``);
# each narrowing intent steps it down, floored so cues stay renderable.
SUBTITLE_BASE_CHARS_PER_LINE: int = 20
SUBTITLE_SHORTER_STEP_CHARS: int = 4
SUBTITLE_MIN_CHARS_PER_LINE: int = 8

SUBTITLE_NARROWING_KINDS: frozenset[str] = frozenset({"subtitle_shorter", "line_wrap"})
"""Kinds that narrow the subtitle wrap width, journal order.

``subtitle_shorter`` is the legacy ambiguous intent (history fact: it
narrowed the width); ``line_wrap`` is its explicit 4-choice successor.
Both step the same cumulative width.
"""

SUBTITLE_NO_KNOB_DETAILS: dict[str, str] = {
    "split_display": (
        "splitting one cue into smaller display chunks is a compile "
        "cue-table change with no review-plane knob; recorded, not rendered"
    ),
    "duration_shorten": (
        "shortening cue display time risks unreadable cues and "
        "speech-timing drift with no review-plane knob; recorded, not rendered"
    ),
    "text_summary_ack": (
        "summarizing spoken content needs an operator acknowledgment and "
        "has no deterministic review-plane knob; the ack is journaled, "
        "the text is never rewritten here"
    ),
}


def subtitle_wrap_widths(narrowing_requests: int) -> tuple[int, ...]:
    """Cumulative wrap widths for N narrowing intents in journal order."""

    widths: list[int] = []
    width = SUBTITLE_BASE_CHARS_PER_LINE
    for _ in range(narrowing_requests):
        width = max(width - SUBTITLE_SHORTER_STEP_CHARS, SUBTITLE_MIN_CHARS_PER_LINE)
        widths.append(width)
    return tuple(widths)


__all__ = [
    "SUBTITLE_BASE_CHARS_PER_LINE",
    "SUBTITLE_MIN_CHARS_PER_LINE",
    "SUBTITLE_NARROWING_KINDS",
    "SUBTITLE_NO_KNOB_DETAILS",
    "SUBTITLE_SHORTER_STEP_CHARS",
    "CueSpan",
    "FrameCueSpan",
    "SampleCueSpan",
    "SubtitleQcPolicy",
    "TranscriptCueSegment",
    "TranscriptCueSource",
    "subtitle_wrap_widths",
]
