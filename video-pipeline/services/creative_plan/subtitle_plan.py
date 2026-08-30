"""Japanese subtitle pipeline v2 orchestration (task 33; PRD §9, impl-plan 8.2).

Deterministic full ladder, no LLM anywhere:

1. ``normalize_timing`` — ASR seconds -> Edit Source frames via the frozen
   ``round_half_away`` assignment rule (``services.conform.rate_model``, the
   same math task 13 reuses); segments are validated against the source
   durations in ``SourceFactsV2`` (unknown source / out-of-source seconds /
   empty or overlapping spans are typed refusals, never silently clamped).
2. text normalization (``subtitle_text``) — punctuation table, explicit
   filler policy, proper-noun dictionary applied post-ASR (no ASR re-run)
   with provenance notes.
3. edit reconciliation (``subtitle_reconcile``) — cues clipped/shifted
   through the IR v2 primary placements; cuts dropped with records.
4. line chunking + reading-speed enforcement (``subtitle_lines``).
5. capability ordering (PRD §9.3) — a PURE function of the mcp-fit matrix
   row for ``subtitle-capability``: accepted -> ``native_text_plus`` first,
   otherwise the external ASS/SRT rung (the legacy mov_text post-render path
   in ``services/resolve_bridge/fixed_presentation_subtitle.py`` stays
   available as that rung's fallback; it is never modified or deleted here).
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.conform.rate_model import round_half_away
from services.contracts.primitives import RecordFrameSpan, SourceFrameSpan
from services.creative_plan.subtitle_capability import (
    PATH_ORDER,
    SubtitlePlanError,
    load_matrix_subtitle_status,
    select_subtitle_path,
)
from services.creative_plan.subtitle_lines import WorkingCue, chunk_cue
from services.creative_plan.subtitle_models import (
    DEFAULT_STYLE_PROFILE,
    AsrSegmentV1,
    ReconciliationRecordV1,
    SubtitleBuildOptions,
    SubtitleDraftCueV1,
    SubtitlePlanCueV1,
    SubtitlePlanV1,
    TextProvenanceNoteV1,
)
from services.creative_plan.subtitle_reconcile import reconcile_after_edit
from services.creative_plan.subtitle_speed import enforce_reading_speed
from services.creative_plan.subtitle_text import (
    FillerPolicy,
    ProperNounDictionaryV1,
    apply_filler_policy,
    apply_proper_nouns,
    load_proper_nouns,
    normalize_punctuation,
    present_fillers,
    punctuation_changes,
)

if TYPE_CHECKING:
    from services.creative_plan.compile_ir_v2 import SourceFactsV2
    from services.creative_plan.ir_models_v2 import TimelineIrV2

__all__ = [
    "DEFAULT_BUILD_OPTIONS",
    "DEFAULT_STYLE_PROFILE",
    "PATH_ORDER",
    "SubtitleBuildOptions",
    "SubtitlePlanError",
    "build_subtitle_plan",
    "load_matrix_subtitle_status",
    "normalize_timing",
    "select_subtitle_path",
]

DEFAULT_BUILD_OPTIONS: Final = SubtitleBuildOptions()


def normalize_timing(
    asr_segments: Sequence[AsrSegmentV1], *, source_facts: SourceFactsV2
) -> tuple[SubtitleDraftCueV1, ...]:
    """Convert ASR seconds to Edit Source frames with the frozen rate rule."""
    durations = {source.source_id: source.duration_frames for source in source_facts.sources}
    rate = source_facts.rate.as_fraction
    drafts: list[SubtitleDraftCueV1] = []
    for segment in asr_segments:
        duration = durations.get(segment.source_id)
        if duration is None:
            raise SubtitlePlanError(
                "unknown-source", f"segment {segment.segment_id} cites unlisted source"
            )
        start = round_half_away(Fraction(segment.start_seconds) * rate)
        end = round_half_away(Fraction(segment.end_seconds) * rate)
        if end <= start:
            raise SubtitlePlanError(
                "empty-timing", f"segment {segment.segment_id} maps to an empty frame span"
            )
        if end > duration:
            raise SubtitlePlanError(
                "asr-span-out-of-source",
                f"segment {segment.segment_id} exceeds source duration {duration} frames",
            )
        drafts.append(
            SubtitleDraftCueV1(
                cue_id=f"cue-{segment.segment_id}",
                transcript_ref=segment.segment_id,
                source_id=segment.source_id,
                text=segment.text,
                start_frame=start,
                end_frame=end,
            )
        )
    ordered = sorted(drafts, key=lambda cue: (cue.source_id, cue.start_frame, cue.cue_id))
    for later, earlier in zip(ordered[1:], ordered, strict=False):
        if later.source_id == earlier.source_id and later.start_frame < earlier.end_frame:
            raise SubtitlePlanError(
                "asr-overlap", f"segments {earlier.cue_id} and {later.cue_id} overlap in source"
            )
    return tuple(sorted(drafts, key=lambda cue: (cue.start_frame, cue.cue_id)))


def _normalize_text(
    draft: SubtitleDraftCueV1,
    *,
    filler_policy: FillerPolicy,
    dictionary: ProperNounDictionaryV1,
) -> tuple[str, tuple[tuple[int, int], ...], list[TextProvenanceNoteV1]]:
    """Punctuation -> filler policy -> proper nouns, with provenance notes."""
    notes: list[TextProvenanceNoteV1] = []
    text = normalize_punctuation(draft.text)
    changes = punctuation_changes(draft.text)
    if changes:
        pairs = ",".join(f"{raw}->{mapped}" for raw, mapped in changes)
        notes.append(
            TextProvenanceNoteV1(
                transcript_ref=draft.transcript_ref,
                action="punctuation_normalized",
                detail=f"applied {pairs}",
            )
        )
    retained = present_fillers(text)
    text, removed = apply_filler_policy(text, filler_policy)
    if filler_policy == "remove" and removed:
        notes.append(
            TextProvenanceNoteV1(
                transcript_ref=draft.transcript_ref,
                action="filler_removed",
                detail=f"removed: {','.join(removed)}",
            )
        )
    elif filler_policy == "retain" and retained:
        notes.append(
            TextProvenanceNoteV1(
                transcript_ref=draft.transcript_ref,
                action="filler_retained",
                detail=f"retained: {','.join(retained)}",
            )
        )
    text, substitutions, atomic = apply_proper_nouns(text, dictionary)
    notes.extend(
        TextProvenanceNoteV1(
            transcript_ref=draft.transcript_ref,
            action="proper_noun_substituted",
            detail=f"{substitution.variant} -> {substitution.canonical}",
        )
        for substitution in substitutions
    )
    return text, atomic, notes


def build_subtitle_plan(
    asr_segments: Sequence[AsrSegmentV1],
    *,
    source_facts: SourceFactsV2,
    ir_v2: TimelineIrV2,
    options: SubtitleBuildOptions = DEFAULT_BUILD_OPTIONS,
) -> SubtitlePlanV1:
    """Run the full deterministic subtitle ladder into a SubtitlePlanV1."""
    style_profile = options.style_profile
    if source_facts.rate != ir_v2.rate:
        raise SubtitlePlanError(
            "rate-mismatch", "source facts rate and IR v2 rate must be identical"
        )
    dictionary = (
        load_proper_nouns() if options.proper_nouns is None else options.proper_nouns
    )
    timed = normalize_timing(asr_segments, source_facts=source_facts)
    provenance: list[TextProvenanceNoteV1] = []
    emptied_records: list[ReconciliationRecordV1] = []
    prepared: list[tuple[SubtitleDraftCueV1, tuple[tuple[int, int], ...]]] = []
    for draft in timed:
        text, atomic, notes = _normalize_text(
            draft, filler_policy=options.filler_policy, dictionary=dictionary
        )
        provenance.extend(notes)
        if not text:
            emptied_records.append(
                ReconciliationRecordV1(
                    cue_id=draft.cue_id,
                    transcript_ref=draft.transcript_ref,
                    action="dropped",
                    detail="empty text after filler removal",
                    source_frames_lost=0,
                )
            )
            continue
        prepared.append((draft.model_copy(update={"text": text}), atomic))
    reconciliation = reconcile_after_edit([cue for cue, _ in prepared], ir_v2)
    atomic_by_cue = {cue.cue_id: atomic for cue, atomic in prepared}
    working: list[WorkingCue] = []
    for cue in reconciliation.cues:
        working.extend(
            chunk_cue(cue, profile=style_profile, atomic_spans=atomic_by_cue[cue.cue_id])
        )
    primary_ends = [
        item.record_span.end_frame
        for track in ir_v2.video_tracks
        if track.role == "primary"
        for item in track.items
    ]
    speed = enforce_reading_speed(
        working,
        rate=source_facts.rate,
        profile=style_profile,
        timeline_end=max(primary_ends, default=0),
    )
    plan_cues = tuple(
        SubtitlePlanCueV1(
            cue_id=cue.cue_id,
            transcript_ref=cue.transcript_ref,
            source_id=cue.source_id,
            lines=cue.lines,
            source_span=SourceFrameSpan(
                start_frame=cue.source_start,
                end_frame=cue.source_end,
                rate=source_facts.rate,
            ),
            record_span=RecordFrameSpan(
                start_frame=cue.record_start, end_frame=cue.record_end
            ),
        )
        for cue in speed.cues
    )
    status = (
        options.matrix_status if options.matrix_status is not None
        else load_matrix_subtitle_status()
    )
    try:
        return SubtitlePlanV1(
            schema_version="subtitle-plan-v1",
            episode_id=ir_v2.episode_id,
            rate=source_facts.rate,
            style_profile=style_profile,
            filler_policy=options.filler_policy,
            cues=plan_cues,
            text_provenance=tuple(provenance),
            reconciliation=(*emptied_records, *reconciliation.records),
            violations=speed.violations,
            capability_path=select_subtitle_path(status),
        )
    except ValidationError as error:
        raise SubtitlePlanError("invalid-plan", str(error)) from error
