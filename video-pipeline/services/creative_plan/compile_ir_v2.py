"""Deterministic CreativeEditPlan -> Timeline IR v2 compiler (task 31).

The IR v2 models live in ``ir_models_v2`` (re-exported here for the public
seam); this module owns compilation. Decision ID linkage: every placed span
carries the MomentCandidateV2 ``candidate_ref`` it came from — compile
refuses refs outside the selection (no orphan placements).

NO Resolve-specific fields or calls anywhere; Resolve execution detail lives
in task 38's McpExecutionPlan. The 0C/v1 contracts module stays frozen.

Compile contract: ``compile_ir_v2(selection, creative, *, source_facts)`` is
a PURE function — record positions are laid sequentially in op authoring
order (ops anchor to candidates placed by EARLIER ops), record length =
handled source length (handles expand the placed span, in-handles clamped at
frame 0), B-roll/cutaways/stills/titles go on their role tracks aligned
inside their anchor's record span, subtitle cues map transcript facts
through the primary source→record correspondence, transitions sit at the
boundary frame of adjacent primary pairs, and J/L-cut split edits shift the
dialogue audio boundary by ±lead frames (video untouched). Same inputs →
identical canonical bytes.

Timeline-scoped ops (music without target, timeline-wide treatments)
resolve after placement against the finished primary span.
"""

# allow: SIZE_OK — task 31 pins the compiler to compile_ir_v2.py; the flat
# 15-case op table is irreducible (one case per PRD 8.11 semantic operation).
# Task-23/29 single-module precedent.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceId,
    SourceRef,
    StrictModel,
)
from services.creative_plan.edit_models_v2 import (
    BRollOverlayOp,
    ColorLookOp,
    CreativeEditPlanProposalV2,
    CutawayOp,
    ManualRequiredOp,
    MusicCueOp,
    PlacePrimaryClipOp,
    PlaceStillOp,
    PunchInOp,
    SfxCueOp,
    SpeedChangeOp,
    SplitEditOp,
    SubtitleTrackOp,
    TitleCardOp,
    TransitionOp,
    VoiceCleanupOp,
)
from services.creative_plan.ir_models_v2 import (
    AudioItemV2,
    AudioTrackV2,
    EffectIntentV2,
    PlacedClipV2,
    SemanticTransitionV2,
    SubtitleCueV2,
    TimelineIrV2,
    VideoTrackV2,
)
from services.editorial_v2.moment_models import (  # noqa: TC001 (runtime lookup)
    MomentCandidateV2,
    MomentSelectionProposalV2,
)

_Frames = Annotated[int, Field(gt=0, strict=True)]


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


# ------------------------------------------------------------ source facts


class SourceFactV2(StrictModel):
    """One Edit Source (CFR Edit Mezzanine) and its length in frames."""

    source_id: SourceId
    duration_frames: _Frames


class TranscriptFactV2(StrictModel):
    """One transcript segment on a source, in Edit Source Frames."""

    segment_id: Identifier
    source_id: SourceId
    text: Annotated[str, Field(min_length=1, strict=True)]
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward_span(self) -> TranscriptFactV2:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "span_empty", "transcript span is a non-empty half-open range"
            )
        return self


class CandidateSourceBindingV2(StrictModel):
    """Explicit candidate → source binding (required with multiple sources)."""

    candidate_ref: Identifier
    source_id: SourceId


class SourceFactsV2(StrictModel):
    """Compile inputs about sources: rate, lengths, transcript segments.

    All Edit Sources share one rate (the timeline rate); per-candidate
    source binding is explicit via ``bindings`` and REQUIRED whenever more
    than one source is present.
    """

    rate: RationalFrameRate
    sources: Annotated[tuple[SourceFactV2, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)
    transcripts: Annotated[tuple[TranscriptFactV2, ...], BeforeValidator(_to_tuple)] = ()
    bindings: Annotated[tuple[CandidateSourceBindingV2, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def require_unique_ids(self) -> SourceFactsV2:
        source_ids = [s.source_id for s in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise PydanticCustomError("duplicate_source", "source ids are unique")
        segment_ids = [t.segment_id for t in self.transcripts]
        if len(set(segment_ids)) != len(segment_ids):
            raise PydanticCustomError("duplicate_segment", "segment ids are unique")
        bound = [b.candidate_ref for b in self.bindings]
        if len(set(bound)) != len(bound):
            raise PydanticCustomError("duplicate_binding", "bindings are per candidate")
        return self


# ------------------------------------------------------------ compiler


class CompileIrV2Error(ValueError):
    """Typed refusal from the compiler (never a silent partial)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _Placement:
    candidate: MomentCandidateV2
    source_id: str
    handled_start: int
    handled_end: int
    record_start: int
    record_end: int

    @property
    def item_id(self) -> str:
        return f"itm-{self.candidate.candidate_id}"


@dataclass
class _AudioDraft:
    placement: _Placement
    record_start: int
    record_end: int
    source_start: int
    source_end: int


@dataclass
class _State:
    candidates: dict[str, MomentCandidateV2]
    facts: SourceFactsV2
    placements: list[_Placement] = field(default_factory=list)
    video: dict[str, list[PlacedClipV2]] = field(default_factory=dict)
    audio: dict[str, list[AudioItemV2]] = field(default_factory=dict)
    cues: list[SubtitleCueV2] = field(default_factory=list)
    transitions: list[SemanticTransitionV2] = field(default_factory=list)
    effects: list[EffectIntentV2] = field(default_factory=list)
    splits: list[tuple[str, str, int]] = field(default_factory=list)
    cursor: int = 0


def compile_ir_v2(
    selection: MomentSelectionProposalV2,
    creative: CreativeEditPlanProposalV2,
    *,
    source_facts: SourceFactsV2,
) -> TimelineIrV2:
    """Deterministically compile selection + creative plan into Timeline IR v2."""

    _require_linkage(selection, creative)
    state = _State(
        candidates={c.candidate_id: c for c in selection.candidates},
        facts=source_facts,
    )
    pending: list[EffectIntentV2 | MusicCueOp] = []
    for op in creative.operations:
        _apply_op(state, op, pending)
    if not state.placements:
        raise CompileIrV2Error("empty-primary", "no place_primary_clip op in the plan")
    _resolve_pending(state, pending)
    return _assemble(selection, creative, state)


def _require_linkage(
    selection: MomentSelectionProposalV2, creative: CreativeEditPlanProposalV2
) -> None:
    if creative.episode_id != selection.episode_id:
        raise CompileIrV2Error(
            "episode-mismatch",
            f"plan episode {creative.episode_id} != selection episode {selection.episode_id}",
        )
    if creative.selection_ref.proposal_id != selection.proposal_id:
        raise CompileIrV2Error(
            "selection-mismatch",
            f"plan extends {creative.selection_ref.proposal_id} but compile "
            f"received {selection.proposal_id}",
        )


def _candidate(state: _State, ref: str, op_id: str) -> MomentCandidateV2:
    candidate = state.candidates.get(ref)
    if candidate is None:
        raise CompileIrV2Error(
            "unknown-candidate", f"op {op_id} references candidate {ref} not in selection"
        )
    return candidate


def _anchor(state: _State, ref: str, op_id: str) -> _Placement:
    for placement in state.placements:
        if placement.candidate.candidate_id == ref:
            return placement
    raise CompileIrV2Error("unknown-anchor", f"op {op_id} anchors to {ref}, not placed on primary")


def _source_for(state: _State, candidate: MomentCandidateV2) -> str:
    for binding in state.facts.bindings:
        if binding.candidate_ref == candidate.candidate_id:
            return binding.source_id
    if len(state.facts.sources) == 1:
        return state.facts.sources[0].source_id
    raise CompileIrV2Error(
        "ambiguous-source",
        f"candidate {candidate.candidate_id} has no source binding among multiple sources",
    )


def _handled(candidate: MomentCandidateV2) -> tuple[int, int]:
    start = candidate.source_span.start_frame
    end = candidate.source_span.end_frame
    if candidate.handles is not None:
        start = max(0, start - candidate.handles.in_frame)
        end += candidate.handles.out_frame
    return start, end


@dataclass(frozen=True, slots=True)
class _ClipSpec:
    """Grouped placement inputs for one clip (keeps helpers under 5 args)."""

    item_id: str
    source_id: str
    source_start: int
    source_end: int
    record_start: int
    candidate_ref: str
    av_link_id: str | None = None


def _clip(
    state: _State,
    spec: _ClipSpec,
) -> PlacedClipV2:
    """Build one placed clip from a grouped spec (source span + record start)."""

    length = spec.source_end - spec.source_start
    return PlacedClipV2(
        item_id=spec.item_id,
        source=SourceRef(
            source_id=spec.source_id,
            span=SourceFrameSpan(
                start_frame=spec.source_start,
                end_frame=spec.source_end,
                rate=state.facts.rate,
            ),
        ),
        record_span=RecordFrameSpan(
            start_frame=spec.record_start, end_frame=spec.record_start + length
        ),
        candidate_ref=spec.candidate_ref,
        av_link_id=spec.av_link_id,
    )


def _apply_op(  # noqa: PLR0912, C901, PLR0915 (flat table: one case per op variant)
    state: _State, op: object, pending: list[EffectIntentV2 | MusicCueOp]
) -> None:
    match op:
        case PlacePrimaryClipOp(candidate_ref=ref, op_id=op_id):
            candidate = _candidate(state, ref, op_id)
            if any(p.candidate.candidate_id == ref for p in state.placements):
                raise CompileIrV2Error(
                    "candidate-reused", f"candidate {ref} placed on primary twice"
                )
            start, end = _handled(candidate)
            placement = _Placement(
                candidate=candidate,
                source_id=_source_for(state, candidate),
                handled_start=start,
                handled_end=end,
                record_start=state.cursor,
                record_end=state.cursor + (end - start),
            )
            state.placements.append(placement)
            state.video.setdefault("primary", []).append(
                _clip(
                    state,
                    _ClipSpec(
                        item_id=placement.item_id,
                        source_id=placement.source_id,
                        source_start=start,
                        source_end=end,
                        record_start=placement.record_start,
                        candidate_ref=ref,
                        av_link_id=f"avl-{ref}",
                    ),
                )
            )
            state.cursor = placement.record_end
        case BRollOverlayOp():
            _overlay(state, op, "b_roll")
        case CutawayOp():
            _overlay(state, op, "insert")
        case PlaceStillOp(
            candidate_ref=ref,
            anchor_candidate_ref=anchor_ref,
            duration_frames=duration,
            op_id=op_id,
        ):
            anchor = _anchor(state, anchor_ref, op_id)
            _candidate(state, ref, op_id)
            state.video.setdefault("still", []).append(
                _clip(
                    state,
                    _ClipSpec(
                        item_id=f"itm-{op_id}",
                        source_id=f"still-{op_id}",
                        source_start=0,
                        source_end=duration,
                        record_start=anchor.record_start,
                        candidate_ref=ref,
                    ),
                )
            )
        case TitleCardOp(target_candidate_ref=ref, duration_frames=duration, op_id=op_id):
            anchor = _anchor(state, ref, op_id)
            state.video.setdefault("graphic", []).append(
                _clip(
                    state,
                    _ClipSpec(
                        item_id=f"itm-{op_id}",
                        source_id=f"graphic-{op_id}",
                        source_start=0,
                        source_end=duration,
                        record_start=anchor.record_start,
                        candidate_ref=anchor.candidate.candidate_id,
                    ),
                )
            )
        case SubtitleTrackOp():
            _subtitles(state, op)
        case TransitionOp(
            a_candidate_ref=a_ref,
            b_candidate_ref=b_ref,
            op_id=op_id,
            style=style,
            duration_frames=duration,
        ):
            boundary = _adjacent_boundary(state, op_id, a_ref, b_ref)
            if any(t.at_record_frame == boundary for t in state.transitions):
                raise CompileIrV2Error(
                    "duplicate-transition", f"two transitions at frame {boundary}"
                )
            state.transitions.append(
                SemanticTransitionV2(
                    transition_id=f"tr-{op_id}",
                    style=style,
                    at_record_frame=boundary,
                    a_item_ref=f"itm-{a_ref}",
                    b_item_ref=f"itm-{b_ref}",
                    duration_frames=duration,
                )
            )
        case SplitEditOp():
            _split_edit(state, op)
        case MusicCueOp():
            if op.target_candidate_ref is None:
                pending.append(op)
                return
            anchor = _anchor(state, op.target_candidate_ref, op.op_id)
            _music_item(state, op, anchor.record_start, anchor.record_end, op.target_candidate_ref)
            if op.ducking:
                state.effects.append(
                    EffectIntentV2(
                        effect_id=f"fx-{op.op_id}", kind="ducking", target_item_id=f"itm-{op.op_id}"
                    )
                )
        case VoiceCleanupOp(target_candidate_ref=ref, treatment=treatment, op_id=op_id):
            if ref is None:
                pending.append(EffectIntentV2(effect_id=f"fx-{op_id}", kind=treatment))
                return
            _anchor(state, ref, op_id)
            state.effects.append(
                EffectIntentV2(effect_id=f"fx-{op_id}", kind=treatment, target_item_id=f"aud-{ref}")
            )
        case SfxCueOp():
            ref, asset, op_id = op.target_candidate_ref, op.asset_ref, op.op_id
            duration = op.duration_frames
            anchor = _anchor(state, ref, op_id)
            state.audio.setdefault("sfx", []).append(
                AudioItemV2(
                    item_id=f"itm-{op_id}",
                    source=SourceRef(
                        source_id=asset if asset is not None else f"sfx-{op_id}",
                        span=SourceFrameSpan(
                            start_frame=0, end_frame=duration, rate=state.facts.rate
                        ),
                    ),
                    record_span=RecordFrameSpan(
                        start_frame=anchor.record_start,
                        end_frame=anchor.record_start + duration,
                    ),
                    candidate_ref=ref,
                )
            )
        case PunchInOp(target_candidate_ref=ref, mode=mode, op_id=op_id):
            _anchor(state, ref, op_id)
            state.effects.append(
                EffectIntentV2(effect_id=f"fx-{op_id}", kind=mode, target_item_id=f"itm-{ref}")
            )
        case ColorLookOp(target_candidate_ref=ref, op_id=op_id):
            if ref is None:
                pending.append(EffectIntentV2(effect_id=f"fx-{op_id}", kind="color_look"))
                return
            _anchor(state, ref, op_id)
            state.effects.append(
                EffectIntentV2(
                    effect_id=f"fx-{op_id}", kind="color_look", target_item_id=f"itm-{ref}"
                )
            )
        case SpeedChangeOp(target_candidate_ref=ref, factor_pct=factor, op_id=op_id):
            _anchor(state, ref, op_id)
            state.effects.append(
                EffectIntentV2(
                    effect_id=f"fx-{op_id}",
                    kind="speed_change",
                    target_item_id=f"itm-{ref}",
                    speed_factor_pct=factor,
                )
            )
        case ManualRequiredOp(target_candidate_ref=ref, effect_note=note, op_id=op_id):
            if ref is None:
                pending.append(
                    EffectIntentV2(effect_id=f"fx-{op_id}", kind="manual_required", note=note)
                )
                return
            _anchor(state, ref, op_id)
            state.effects.append(
                EffectIntentV2(
                    effect_id=f"fx-{op_id}",
                    kind="manual_required",
                    target_item_id=f"itm-{ref}",
                    note=note,
                )
            )
        case _:
            raise CompileIrV2Error("unknown-op", f"unhandled operation {op!r}")


def _overlay(state: _State, op: BRollOverlayOp | CutawayOp, role: str) -> None:
    op_id, ref, anchor_ref = op.op_id, op.candidate_ref, op.anchor_candidate_ref
    anchor = _anchor(state, anchor_ref, op_id)
    candidate = _candidate(state, ref, op_id)
    start, end = _handled(candidate)
    length = min(anchor.record_end - anchor.record_start, end - start)
    state.video.setdefault(role, []).append(
        _clip(
            state,
            _ClipSpec(
                item_id=f"itm-{op_id}",
                source_id=_source_for(state, candidate),
                source_start=start,
                source_end=start + length,
                record_start=anchor.record_start,
                candidate_ref=ref,
            ),
        )
    )


def _subtitles(state: _State, op: SubtitleTrackOp) -> None:
    op_id, ref, segment = op.op_id, op.target_candidate_ref, op.transcript_ref
    anchor = _anchor(state, ref, op_id)
    content_start = anchor.candidate.source_span.start_frame
    content_end = anchor.candidate.source_span.end_frame
    segments = [
        fact
        for fact in state.facts.transcripts
        if fact.source_id == anchor.source_id
        and fact.start_frame < content_end
        and fact.end_frame > content_start
    ]
    if segment is not None:
        segments = [fact for fact in segments if fact.segment_id == segment]
        if not segments:
            raise CompileIrV2Error(
                "unknown-transcript", f"op {op_id} cites segment {segment} not in facts"
            )
    for fact in sorted(segments, key=lambda f: (f.start_frame, f.segment_id)):
        start = max(fact.start_frame, content_start)
        end = min(fact.end_frame, content_end)
        record_start = anchor.record_start + (start - anchor.handled_start)
        state.cues.append(
            SubtitleCueV2(
                cue_id=f"cue-{fact.segment_id}",
                text=fact.text,
                record_span=RecordFrameSpan(
                    start_frame=record_start, end_frame=record_start + (end - start)
                ),
                candidate_ref=ref,
                transcript_ref=fact.segment_id,
            )
        )


def _adjacent_boundary(state: _State, op_id: str, a_ref: str, b_ref: str) -> int:
    a_index = next(
        (i for i, p in enumerate(state.placements) if p.candidate.candidate_id == a_ref),
        None,
    )
    b_index = next(
        (i for i, p in enumerate(state.placements) if p.candidate.candidate_id == b_ref),
        None,
    )
    if a_index is None or b_index is None or b_index != a_index + 1:
        raise CompileIrV2Error(
            "transition-boundary",
            f"op {op_id} needs {a_ref} and {b_ref} adjacent on primary",
        )
    return state.placements[a_index].record_end


def _split_edit(state: _State, op: SplitEditOp) -> None:
    op_id, a_ref, b_ref = op.op_id, op.a_candidate_ref, op.b_candidate_ref
    _adjacent_boundary(state, op_id, a_ref, b_ref)  # adjacency is the contract
    a = _anchor(state, a_ref, op_id)
    b = _anchor(state, b_ref, op_id)
    delta = -op.lead_frames if op.style == "j_cut" else op.lead_frames
    if delta < 0 and b.handled_start + delta < 0:
        raise CompileIrV2Error(
            "split-edit-material",
            f"op {op_id} needs source frames before {b.handled_start}",
        )
    if delta > 0:
        duration = next(
            s.duration_frames for s in state.facts.sources if s.source_id == a.source_id
        )
        if a.handled_end + delta > duration:
            raise CompileIrV2Error(
                "split-edit-material",
                f"op {op_id} needs source frames beyond {a.handled_end}",
            )
    if (a_ref, b_ref) in state.splits:
        raise CompileIrV2Error(
            "duplicate-split-edit", f"two split edits between {a_ref} and {b_ref}"
        )
    state.splits.append((a_ref, b_ref, delta))


def _music_item(
    state: _State, op: MusicCueOp, record_start: int, record_end: int, ref: str
) -> None:
    state.audio.setdefault("music", []).append(
        AudioItemV2(
            item_id=f"itm-{op.op_id}",
            source=SourceRef(
                source_id=op.asset_ref if op.asset_ref is not None else f"music-{op.op_id}",
                span=SourceFrameSpan(
                    start_frame=0,
                    end_frame=record_end - record_start,
                    rate=state.facts.rate,
                ),
            ),
            record_span=RecordFrameSpan(start_frame=record_start, end_frame=record_end),
            candidate_ref=ref,
        )
    )


def _resolve_pending(state: _State, pending: list[EffectIntentV2 | MusicCueOp]) -> None:
    """Timeline-scoped ops resolve against the finished primary span."""
    anchor_ref = state.placements[0].candidate.candidate_id
    for item in pending:
        if isinstance(item, MusicCueOp):
            _music_item(state, item, 0, state.cursor, anchor_ref)
            if item.ducking:
                state.effects.append(
                    EffectIntentV2(
                        effect_id=f"fx-{item.op_id}",
                        kind="ducking",
                        target_item_id=f"itm-{item.op_id}",
                    )
                )
        else:
            state.effects.append(item)


def _assemble(
    selection: MomentSelectionProposalV2,
    creative: CreativeEditPlanProposalV2,
    state: _State,
) -> TimelineIrV2:
    drafts = [
        _AudioDraft(
            placement=p,
            record_start=p.record_start,
            record_end=p.record_end,
            source_start=p.handled_start,
            source_end=p.handled_end,
        )
        for p in state.placements
    ]
    for draft in drafts:
        for a_ref, b_ref, delta in state.splits:
            if draft.placement.candidate.candidate_id == a_ref:
                draft.record_end += delta
                draft.source_end += delta
            if draft.placement.candidate.candidate_id == b_ref:
                draft.record_start += delta
                draft.source_start += delta
        if draft.record_end <= draft.record_start:
            raise CompileIrV2Error(
                "split-edit-empty",
                f"split edit empties the audio span of {draft.placement.candidate.candidate_id}",
            )
    dialogue = [
        AudioItemV2(
            item_id=f"aud-{draft.placement.candidate.candidate_id}",
            source=SourceRef(
                source_id=draft.placement.source_id,
                span=SourceFrameSpan(
                    start_frame=draft.source_start,
                    end_frame=draft.source_end,
                    rate=state.facts.rate,
                ),
            ),
            record_span=RecordFrameSpan(start_frame=draft.record_start, end_frame=draft.record_end),
            candidate_ref=draft.placement.candidate.candidate_id,
            av_link_id=f"avl-{draft.placement.candidate.candidate_id}",
        )
        for draft in drafts
    ]
    if dialogue:
        state.audio.setdefault("dialogue", []).extend(dialogue)
    video_order = ("primary", "b_roll", "insert", "still", "graphic")
    audio_order = ("dialogue", "music", "ambience", "sfx")
    try:
        return TimelineIrV2(
            schema_version="timeline-ir-v2",
            episode_id=selection.episode_id,
            rate=state.facts.rate,
            video_tracks=tuple(
                VideoTrackV2(role=role, track_id=f"v-{role}", items=tuple(state.video[role]))
                for role in video_order
                if state.video.get(role)
            ),
            subtitle_cues=tuple(
                sorted(state.cues, key=lambda cue: (cue.record_span.start_frame, cue.cue_id))
            ),
            audio_tracks=tuple(
                AudioTrackV2(role=role, track_id=f"a-{role}", items=tuple(state.audio[role]))
                for role in audio_order
                if state.audio.get(role)
            ),
            transitions=tuple(state.transitions),
            effect_intents=tuple(state.effects),
            presentation_intent_refs=tuple(
                record.intent_id for record in creative.presentation_intents
            ),
        )
    except ValidationError as error:
        raise CompileIrV2Error("invalid-layout", str(error)) from error


__all__ = [
    "AudioItemV2",
    "AudioTrackV2",
    "CandidateSourceBindingV2",
    "CompileIrV2Error",
    "EffectIntentV2",
    "PlacedClipV2",
    "SemanticTransitionV2",
    "SourceFactV2",
    "SourceFactsV2",
    "SubtitleCueV2",
    "TimelineIrV2",
    "TranscriptFactV2",
    "VideoTrackV2",
    "compile_ir_v2",
]
