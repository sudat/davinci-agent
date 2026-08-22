"""Editorial QC candidate generation — task 41; PRD 14.2 (all ten checks).

Every function here is DETERMINISTIC (no LLM, no Resolve, no network): the
inputs are committed plan/IR artifacts plus the preview render trace, and the
outputs are ``EditorialQcCandidateV1`` records — REVIEW INPUT, never final
truth (PRD 14.2). Critical candidates ALWAYS carry
``needs_human_review=True`` (enforced at the candidate AND report models, so
hand-built or serialized payloads cannot smuggle an unreviewed critical).

Check inventory (PRD 14.2 complete), with its evidence seam:

- ``duplicated_explanation``  — char-bigram Jaccard over IR subtitle cue
  texts (the transcript surface rendered into the edit); pairs at or above
  the similarity threshold.
- ``narrative_jump``          — kept selection candidates vs ``StoryPlanV1``:
  a block ref missing from the plan (critical) or a plan block whose order
  sits strictly between edited blocks yet is referenced by no kept candidate.
- ``awkward_cut``             — T22 ``MomentHandles`` vs the IR primary
  placements (compiler contract: placed span = content span expanded by
  handles, in-handle clamped at 0): a placement cutting INTO the content
  span (warning) or losing requested handles (info).
- ``broll_irrelevance``       — T31 ``BRollOverlayOp`` B-roll rationale vs
  anchor rationale token overlap below the threshold.
- ``subtitle_mismatch``       — T33 ``ReconciliationRecordV1`` (dropped
  cues; trimmed beyond frame tolerance) plus the preview-trace subtitle
  presence seam (plan carries cues, render sampled none).
- ``effect_density_excess``   — re-runs the T32 ``check_density`` guard as
  QC (no reimplementation); violations map 1:1 to candidates.
- ``long_low_value_segment``  — T16 ``TriageEntry`` router scores joined to
  placements via candidate ``evidence_refs`` shot ids; a span at or beyond
  the length threshold whose BEST corroborated score is still low.
- ``audio_transition_problem``— IR audio-track boundary gaps without a
  covering ``SemanticTransitionV2``, plus the T34 seam: ``bgm_placement``
  enabled in ``AudioFinishingPlanV1`` while the IR carries no music span.
- ``sensitive_private_content`` — keyword/config FLAG ONLY, low confidence,
  human review mandatory. This complements — never replaces — the
  declaration-based privacy gate (``services/qc/privacy_gate.py``): a hit is
  a review candidate, an absence proves nothing.
- ``unsupported_manual_finalization`` — manual-required items surfaced from
  the IR effect intents and the creative plan (PRD 14.3 Cockpit surfacing).

Identity joins require the artifacts the compiler guarantees: placements'
``candidate_ref`` must resolve into the selection (orphans are task-29
validation's domain and are skipped here, never guessed).

``to_review_context`` is the documented integration seam to task 47: the
structured payload carries ``ReviewCommandKind`` suggestions and player
seconds that the review-command parser/wiring can consume (no UI here).
"""

# allow: SIZE_OK — the plan pins this deliverable to the single module
# services/qc/editorial_checks.py (commit scope: this file + its test); the
# ten PRD 14.2 checks plus their candidate/report models are one
# responsibility (editorial candidate generation). Task-31/47 SIZE_OK
# precedent.

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    ResolveFreeModel,
    StrictModel,
)
from services.creative_plan.audio_finishing import (
    AudioFinishingPlanV1,  # noqa: TC001 (pydantic runtime)
)
from services.creative_plan.edit_models_v2 import (  # noqa: TC001 (pydantic runtime)
    CreativeEditPlanProposalV2,
)
from services.creative_plan.ir_models_v2 import (  # noqa: TC001 (pydantic runtime)
    TimelineIrV2,
    VideoTrackV2,
)
from services.creative_plan.presentation_intents import (
    ChannelPresentationProfile,
    DensityLimitExceededError,
    PresentationIntentV2,
    check_density,
    load_default_profile,
)
from services.creative_plan.subtitle_models import (  # noqa: TC001 (pydantic runtime)
    SubtitlePlanV1,
)
from services.editorial_v2.moment_models import (  # noqa: TC001 (pydantic runtime)
    MomentCandidateV2,
    MomentSelectionProposalV2,
)
from services.editorial_v2.story_plan import StoryPlanV1  # noqa: TC001 (pydantic runtime)
from services.episode_cockpit.review_chat import ReviewCommandKind  # noqa: TC001 (pydantic runtime)
from services.media_intelligence.progressive import (  # noqa: TC001 (pydantic runtime)
    TriageEntry,
)
from services.preview.models import PreviewTraceManifest  # noqa: TC001 (pydantic runtime)

EDITORIAL_QC_ENGINE_VERSION: Final = "editorial-qc-v1"

_ORDER_PAIR_MIN: Final = 2

EditorialQcCheck = Literal[
    "duplicated_explanation",
    "narrative_jump",
    "awkward_cut",
    "broll_irrelevance",
    "subtitle_mismatch",
    "effect_density_excess",
    "long_low_value_segment",
    "audio_transition_problem",
    "sensitive_private_content",
    "unsupported_manual_finalization",
]
ALL_EDITORIAL_CHECKS: tuple[EditorialQcCheck, ...] = (
    "duplicated_explanation",
    "narrative_jump",
    "awkward_cut",
    "broll_irrelevance",
    "subtitle_mismatch",
    "effect_density_excess",
    "long_low_value_segment",
    "audio_transition_problem",
    "sensitive_private_content",
    "unsupported_manual_finalization",
)
EditorialQcSeverity = Literal["info", "warning", "critical"]

DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    r"\d{2,4}-\d{3,4}-\d{3,4}",  # phone-like numbers
    r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{2,4}\b",  # card-like numbers
    "マイナンバー|住民票コード",
)


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


# ------------------------------------------------------------- models


class EditorialQcCandidateV1(ResolveFreeModel):
    """One editorial QC finding — review input, never final truth."""

    check: EditorialQcCheck
    detail: Annotated[str, Field(min_length=1, strict=True)]
    record_span: RecordFrameSpan | None = None
    item_refs: Annotated[tuple[Identifier, ...], BeforeValidator(_to_tuple)] = ()
    severity: EditorialQcSeverity
    needs_human_review: bool = False
    evidence_refs: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)

    @model_validator(mode="after")
    def require_critical_review(self) -> EditorialQcCandidateV1:
        if self.severity == "critical" and not self.needs_human_review:
            raise PydanticCustomError(
                "critical_requires_review",
                "critical candidates always need human review: {check}",
                {"check": self.check},
            )
        return self


class EditorialQcReportV1(ResolveFreeModel):
    """Aggregated editorial QC candidates with count + review invariants."""

    schema_version: Literal["editorial-qc-report-v1"]
    candidates: Annotated[tuple[EditorialQcCandidateV1, ...], BeforeValidator(_to_tuple)] = ()
    counts_by_severity: dict[EditorialQcSeverity, int]
    counts_by_check: dict[EditorialQcCheck, int]

    @model_validator(mode="after")
    def require_counts_and_review_invariants(self) -> EditorialQcReportV1:
        severity: dict[str, int] = {"info": 0, "warning": 0, "critical": 0}
        by_check: dict[str, int] = {}
        for candidate in self.candidates:
            severity[candidate.severity] += 1
            by_check[candidate.check] = by_check.get(candidate.check, 0) + 1
            if candidate.severity == "critical" and not candidate.needs_human_review:
                raise PydanticCustomError(
                    "critical_requires_review",
                    "critical candidates always need human review: {check}",
                    {"check": candidate.check},
                )
        if self.counts_by_severity != severity or self.counts_by_check != dict(
            sorted(by_check.items())
        ):
            raise PydanticCustomError(
                "counts_mismatch",
                "declared counts must match the candidate list: "
                "severity {severity} / check {by_check}",
                {"severity": severity, "by_check": sorted(by_check.items())},
            )
        return self


class EditorialQcThresholds(StrictModel):
    """Deterministic thresholds + the sensitive-pattern config (PRD 14.2)."""

    duplication_similarity_min: float = Field(default=0.8, ge=0.0, le=1.0, strict=True)
    broll_relevance_min_overlap: float = Field(default=0.15, ge=0.0, le=1.0, strict=True)
    subtitle_trim_tolerance_frames: int = Field(default=12, ge=0, strict=True)
    low_value_min_span_frames: int = Field(default=450, gt=0, strict=True)
    low_value_max_router_score: float = Field(default=0.3, ge=0.0, le=1.0, strict=True)
    audio_gap_tolerance_frames: int = Field(default=2, ge=0, strict=True)
    sensitive_patterns: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(
        default=DEFAULT_SENSITIVE_PATTERNS, min_length=1
    )

    @field_validator("sensitive_patterns")
    @classmethod
    def require_compilable_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as error:
                raise PydanticCustomError(
                    "pattern_invalid",
                    "sensitive pattern {pattern} is not a valid regex: {error}",
                    {"pattern": pattern, "error": str(error)},
                ) from error
        return value


DEFAULT_EDITORIAL_QC_THRESHOLDS: Final[EditorialQcThresholds] = EditorialQcThresholds()


class EditorialQcInput(StrictModel):
    """The committed artifacts every check runs over (all joins optional)."""

    ir_v2: TimelineIrV2
    preview_trace: PreviewTraceManifest | None = None
    subtitle_plan: SubtitlePlanV1 | None = None
    selection: MomentSelectionProposalV2 | None = None
    creative_plan: CreativeEditPlanProposalV2 | None = None
    story_plan: StoryPlanV1 | None = None
    triage: Annotated[tuple[TriageEntry, ...], BeforeValidator(_to_tuple)] = ()
    presentation_intents: Annotated[
        tuple[PresentationIntentV2, ...], BeforeValidator(_to_tuple)
    ] = ()
    presentation_profile: ChannelPresentationProfile | None = None
    audio_plan: AudioFinishingPlanV1 | None = None


# ------------------------------------------------------- shared helpers


def _bigrams(text: str) -> frozenset[str]:
    normalized = "".join(text.split())
    if not normalized:
        return frozenset()
    if len(normalized) == 1:
        return frozenset((normalized,))
    return frozenset(normalized[index : index + 2] for index in range(len(normalized) - 1))


def _similarity(a: str, b: str) -> float:
    grams_a, grams_b = _bigrams(a), _bigrams(b)
    if not grams_a or not grams_b:
        return 0.0
    return len(grams_a & grams_b) / len(grams_a | grams_b)


def _primary_track(ir: TimelineIrV2) -> VideoTrackV2 | None:
    return next((track for track in ir.video_tracks if track.role == "primary"), None)


def _candidates_by_id(
    inputs: EditorialQcInput,
) -> dict[str, MomentCandidateV2]:
    if inputs.selection is None:
        return {}
    return {candidate.candidate_id: candidate for candidate in inputs.selection.candidates}


def _span_of(candidate: EditorialQcCandidateV1) -> int:
    return candidate.record_span.start_frame if candidate.record_span is not None else 0


# ------------------------------------------------------------- checks


def check_duplicated_explanation(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Near-identical cue texts (char-bigram Jaccard) at disjoint spans."""

    cues = inputs.ir_v2.subtitle_cues
    found: list[EditorialQcCandidateV1] = []
    for i, earlier in enumerate(cues):
        for later in cues[i + 1 :]:
            similarity = _similarity(earlier.text, later.text)
            if similarity < thresholds.duplication_similarity_min:
                continue
            found.append(
                EditorialQcCandidateV1(
                    check="duplicated_explanation",
                    detail=(
                        f"cues {earlier.cue_id} and {later.cue_id} are near-identical "
                        f"(similarity {similarity:.3f} >= "
                        f"{thresholds.duplication_similarity_min})"
                    ),
                    record_span=later.record_span,
                    item_refs=(earlier.cue_id, later.cue_id),
                    severity="warning",
                    evidence_refs=(f"ir:{earlier.cue_id}", f"ir:{later.cue_id}"),
                )
            )
    return tuple(found)


def check_narrative_jump(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Story-block discontinuity: missing referenced block / skipped block."""

    del thresholds  # threshold-free structural check
    if inputs.selection is None or inputs.story_plan is None:
        return ()
    blocks = {block.block_id: block for block in inputs.story_plan.blocks}
    kept = [candidate for candidate in inputs.selection.candidates if candidate.intent == "keep"]
    primary = _primary_track(inputs.ir_v2)
    found: list[EditorialQcCandidateV1] = []
    for candidate in kept:
        ref = candidate.story_block_ref
        if ref is None or ref in blocks:
            continue
        span = None
        if primary is not None:
            placement = next(
                (item for item in primary.items if item.candidate_ref == candidate.candidate_id),
                None,
            )
            span = placement.record_span if placement is not None else None
        found.append(
            EditorialQcCandidateV1(
                check="narrative_jump",
                detail=(
                    f"kept candidate {candidate.candidate_id} references story block "
                    f"{ref} which is missing from the story plan"
                ),
                record_span=span,
                item_refs=(candidate.candidate_id,),
                severity="critical",
                needs_human_review=True,
                evidence_refs=(f"selection:{candidate.candidate_id}", f"story:{ref}"),
            )
        )
    referenced_orders = sorted(
        {
            blocks[candidate.story_block_ref].order
            for candidate in kept
            if candidate.story_block_ref is not None and candidate.story_block_ref in blocks
        }
    )
    if len(referenced_orders) >= _ORDER_PAIR_MIN:
        for block in inputs.story_plan.blocks:
            if block.order in referenced_orders:
                continue
            if referenced_orders[0] < block.order < referenced_orders[-1]:
                found.append(
                    EditorialQcCandidateV1(
                        check="narrative_jump",
                        detail=(
                            f"story block {block.block_id} (order {block.order}) sits "
                            "between edited blocks but no kept candidate references it"
                        ),
                        severity="warning",
                        evidence_refs=(f"story:{block.block_id}",),
                    )
                )
    return tuple(found)


def check_awkward_cut(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """T22 handles vs IR primary placements (compiler handle contract)."""

    del thresholds  # exact structural comparison; no threshold
    primary = _primary_track(inputs.ir_v2)
    if primary is None or inputs.selection is None:
        return ()
    by_id = _candidates_by_id(inputs)
    found: list[EditorialQcCandidateV1] = []
    for placement in primary.items:
        candidate = by_id.get(placement.candidate_ref)
        if candidate is None:
            continue
        content = candidate.source_span
        placed = placement.source.span
        in_handle = candidate.handles.in_frame if candidate.handles else 0
        out_handle = candidate.handles.out_frame if candidate.handles else 0
        expected_start = max(0, content.start_frame - in_handle)
        expected_end = content.end_frame + out_handle
        if placed.start_frame > content.start_frame or placed.end_frame < content.end_frame:
            severity: EditorialQcSeverity = "warning"
            detail = (
                f"placement {placement.item_id} cuts into the content span of "
                f"{candidate.candidate_id}: placed [{placed.start_frame},{placed.end_frame}) "
                f"vs content [{content.start_frame},{content.end_frame})"
            )
        elif placed.start_frame > expected_start or placed.end_frame < expected_end:
            severity = "info"
            detail = (
                f"placement {placement.item_id} drops requested handles of "
                f"{candidate.candidate_id}: placed [{placed.start_frame},{placed.end_frame}) "
                f"vs handled [{expected_start},{expected_end})"
            )
        else:
            continue
        found.append(
            EditorialQcCandidateV1(
                check="awkward_cut",
                detail=detail,
                record_span=placement.record_span,
                item_refs=(placement.item_id,),
                severity=severity,
                evidence_refs=(
                    f"ir:{placement.item_id}",
                    f"selection:{candidate.candidate_id}",
                ),
            )
        )
    return tuple(found)


def check_broll_irrelevance(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """B-roll overlay rationale vs anchor rationale token overlap."""

    if inputs.creative_plan is None:
        return ()
    by_id = _candidates_by_id(inputs)
    b_roll_items = [
        item
        for track in inputs.ir_v2.video_tracks
        if track.role == "b_roll"
        for item in track.items
    ]
    found: list[EditorialQcCandidateV1] = []
    for op in inputs.creative_plan.operations:
        if op.kind != "b_roll_overlay":
            continue
        b_roll = by_id.get(op.candidate_ref)
        anchor = by_id.get(op.anchor_candidate_ref)
        if b_roll is None or anchor is None:
            continue
        overlap = _similarity(b_roll.rationale, anchor.rationale)
        if overlap >= thresholds.broll_relevance_min_overlap:
            continue
        overlay = next(
            (item for item in b_roll_items if item.candidate_ref == b_roll.candidate_id),
            None,
        )
        found.append(
            EditorialQcCandidateV1(
                check="broll_irrelevance",
                detail=(
                    f"B-roll {b_roll.candidate_id} rationale overlaps anchor "
                    f"{anchor.candidate_id} only {overlap:.3f} < "
                    f"{thresholds.broll_relevance_min_overlap}"
                ),
                record_span=overlay.record_span if overlay is not None else None,
                item_refs=(op.op_id, b_roll.candidate_id),
                severity="warning",
                evidence_refs=(
                    f"plan:{op.op_id}",
                    f"selection:{b_roll.candidate_id}",
                    f"selection:{anchor.candidate_id}",
                ),
            )
        )
    return tuple(found)


def check_subtitle_mismatch(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """T33 reconciliation records + preview-render subtitle presence."""

    plan = inputs.subtitle_plan
    if plan is None:
        return ()
    found: list[EditorialQcCandidateV1] = []
    for record in plan.reconciliation:
        if record.action == "dropped":
            found.append(
                EditorialQcCandidateV1(
                    check="subtitle_mismatch",
                    detail=(
                        f"cue {record.cue_id} was dropped by the edit "
                        f"({record.source_frames_lost} source frames lost)"
                    ),
                    severity="info",
                    evidence_refs=(f"subtitle:{record.cue_id}",),
                )
            )
        elif (
            record.action == "trimmed"
            and record.source_frames_lost > thresholds.subtitle_trim_tolerance_frames
        ):
            found.append(
                EditorialQcCandidateV1(
                    check="subtitle_mismatch",
                    detail=(
                        f"cue {record.cue_id} was trimmed by {record.source_frames_lost} "
                        f"frames beyond tolerance "
                        f"{thresholds.subtitle_trim_tolerance_frames}"
                    ),
                    severity="warning",
                    evidence_refs=(f"subtitle:{record.cue_id}",),
                )
            )
    trace = inputs.preview_trace
    if (
        trace is not None
        and plan.cues
        and not any(entry.kind == "subtitle" for entry in trace.inputs)
    ):
        found.append(
            EditorialQcCandidateV1(
                check="subtitle_mismatch",
                detail=(
                    f"subtitle plan carries {len(plan.cues)} cues but the preview "
                    "render sampled no subtitle track"
                ),
                severity="warning",
                evidence_refs=("preview-trace:no-subtitle-input",),
            )
        )
    return tuple(found)


def check_effect_density_excess(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Re-run the T32 density guard as QC (no reimplementation)."""

    del thresholds  # caps live in the presentation profile
    if not inputs.presentation_intents:
        return ()
    profile = (
        inputs.presentation_profile
        if inputs.presentation_profile is not None
        else load_default_profile()
    )
    primary = _primary_track(inputs.ir_v2)
    if primary is None:
        return ()
    duration_seconds = float(
        max(item.record_span.end_frame for item in primary.items) / inputs.ir_v2.rate.as_fraction
    )
    try:
        check_density(
            inputs.presentation_intents,
            profile,
            timeline_duration_seconds=duration_seconds,
        )
    except DensityLimitExceededError as error:
        return tuple(
            EditorialQcCandidateV1(
                check="effect_density_excess",
                detail=(
                    f"{violation.kind or 'global'} density {violation.observed_per_minute:.2f}"
                    f"/min over cap {violation.cap_per_minute:.2f}/min "
                    f"({violation.count} intents)"
                ),
                severity="warning",
                evidence_refs=(f"policy:density:{violation.kind or 'global'}",),
            )
            for violation in error.report.violations
        )
    return ()


def check_long_low_value_segment(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Long primary spans whose corroborated T16 router scores are all low."""

    primary = _primary_track(inputs.ir_v2)
    if primary is None or inputs.selection is None or not inputs.triage:
        return ()
    by_id = _candidates_by_id(inputs)
    triage_by_shot = {entry.shot_id: entry for entry in inputs.triage}
    found: list[EditorialQcCandidateV1] = []
    for placement in primary.items:
        if placement.record_span.length < thresholds.low_value_min_span_frames:
            continue
        candidate = by_id.get(placement.candidate_ref)
        if candidate is None:
            continue
        matched = [triage_by_shot[ref] for ref in candidate.evidence_refs if ref in triage_by_shot]
        if not matched:
            continue
        best = max(entry.router_score for entry in matched)
        if best > thresholds.low_value_max_router_score:
            continue
        found.append(
            EditorialQcCandidateV1(
                check="long_low_value_segment",
                detail=(
                    f"primary item {placement.item_id} holds "
                    f"{placement.record_span.length} frames whose triage router "
                    f"scores peak at {best:.2f} <= {thresholds.low_value_max_router_score}"
                ),
                record_span=placement.record_span,
                item_refs=(placement.item_id,),
                severity="info",
                evidence_refs=(
                    f"ir:{placement.item_id}",
                    *(f"triage:{entry.shot_id}" for entry in matched),
                ),
            )
        )
    return tuple(found)


def check_audio_transition_problem(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Audio boundary gaps without covering transitions + the T34 BGM seam."""

    found: list[EditorialQcCandidateV1] = []
    for track in inputs.ir_v2.audio_tracks:
        ordered = sorted(track.items, key=lambda item: (item.record_span.start_frame, item.item_id))
        for earlier, later in pairwise(ordered):
            gap_start = earlier.record_span.end_frame
            gap_end = later.record_span.start_frame
            if gap_end - gap_start <= thresholds.audio_gap_tolerance_frames:
                continue
            covered = any(
                gap_start <= transition.at_record_frame < gap_end
                for transition in inputs.ir_v2.transitions
            )
            if covered:
                continue
            found.append(
                EditorialQcCandidateV1(
                    check="audio_transition_problem",
                    detail=(
                        f"{track.role} audio gap of {gap_end - gap_start} frames between "
                        f"{earlier.item_id} and {later.item_id} with no covering transition"
                    ),
                    record_span=RecordFrameSpan(start_frame=gap_start, end_frame=gap_end),
                    item_refs=(earlier.item_id, later.item_id),
                    severity="warning",
                    evidence_refs=(f"ir:{earlier.item_id}", f"ir:{later.item_id}"),
                )
            )
    if inputs.audio_plan is not None:
        bgm = next(
            (stage for stage in inputs.audio_plan.stages if stage.stage == "bgm_placement"),
            None,
        )
        has_music = any(track.role == "music" for track in inputs.ir_v2.audio_tracks)
        if bgm is not None and bgm.enabled and not has_music:
            found.append(
                EditorialQcCandidateV1(
                    check="audio_transition_problem",
                    detail=(
                        "audio finishing enables BGM placement but the IR carries "
                        "no music audio span"
                    ),
                    severity="warning",
                    evidence_refs=("audio-plan:bgm_placement",),
                )
            )
    return tuple(found)


def check_sensitive_private_content(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Keyword FLAG ONLY — low confidence; human review is mandatory."""

    patterns = tuple(
        re.compile(pattern, re.IGNORECASE) for pattern in thresholds.sensitive_patterns
    )
    primary = _primary_track(inputs.ir_v2)
    by_id = _candidates_by_id(inputs)
    surfaces: list[tuple[str, str, RecordFrameSpan | None, str]] = [
        (cue.text, f"ir:{cue.cue_id}", cue.record_span, cue.cue_id)
        for cue in inputs.ir_v2.subtitle_cues
    ]
    if primary is not None:
        for placement in primary.items:
            candidate = by_id.get(placement.candidate_ref)
            if candidate is not None:
                surfaces.append(
                    (
                        candidate.rationale,
                        f"selection:{candidate.candidate_id}",
                        placement.record_span,
                        candidate.candidate_id,
                    )
                )
    found: list[EditorialQcCandidateV1] = []
    for text, evidence, span, item_id in surfaces:
        for index, pattern in enumerate(patterns):
            if pattern.search(text) is None:
                continue
            found.append(
                EditorialQcCandidateV1(
                    check="sensitive_private_content",
                    detail=(
                        f"pattern {index} matched {evidence}: possible sensitive or "
                        "private content — flag only, human confirmation required"
                    ),
                    record_span=span,
                    item_refs=(item_id,),
                    severity="warning",
                    needs_human_review=True,
                    evidence_refs=(evidence, f"pattern:{index}"),
                )
            )
    return tuple(found)


def check_unsupported_manual_finalization(
    inputs: EditorialQcInput, thresholds: EditorialQcThresholds
) -> tuple[EditorialQcCandidateV1, ...]:
    """Surface manual-required items from the IR and the creative plan."""

    del thresholds  # structural surfacing; no threshold
    spans_by_item = {
        item.item_id: item.record_span
        for track in inputs.ir_v2.video_tracks + inputs.ir_v2.audio_tracks
        for item in track.items
    }
    found: list[EditorialQcCandidateV1] = []
    for effect in inputs.ir_v2.effect_intents:
        if effect.kind != "manual_required":
            continue
        found.append(
            EditorialQcCandidateV1(
                check="unsupported_manual_finalization",
                detail=(
                    f"IR effect {effect.effect_id} requires manual finalization"
                    + (f": {effect.note}" if effect.note else "")
                ),
                record_span=(
                    spans_by_item[effect.target_item_id]
                    if effect.target_item_id is not None and effect.target_item_id in spans_by_item
                    else None
                ),
                item_refs=(effect.effect_id,),
                severity="info",
                needs_human_review=True,
                evidence_refs=(f"ir:{effect.effect_id}",),
            )
        )
    if inputs.creative_plan is not None:
        for op in inputs.creative_plan.operations:
            if op.kind != "manual_required":
                continue
            found.append(
                EditorialQcCandidateV1(
                    check="unsupported_manual_finalization",
                    detail=(
                        f"creative plan op {op.op_id} requires manual finalization: "
                        f"{op.effect_note}"
                    ),
                    severity="info",
                    needs_human_review=True,
                    evidence_refs=(f"plan:{op.op_id}",),
                )
            )
    return tuple(found)


# ------------------------------------------------------------- orchestration


def run_editorial_qc(
    inputs: EditorialQcInput,
    thresholds: EditorialQcThresholds = DEFAULT_EDITORIAL_QC_THRESHOLDS,
) -> tuple[EditorialQcCandidateV1, ...]:
    """Run all ten PRD 14.2 checks; deterministic (check, span, detail) order."""

    found: list[EditorialQcCandidateV1] = []
    for check in ALL_EDITORIAL_CHECKS:
        runner = globals()[f"check_{check}"]
        found.extend(runner(inputs, thresholds))
    return tuple(sorted(found, key=lambda c: (c.check, _span_of(c), c.detail)))


def aggregate_candidates(
    candidates: Sequence[EditorialQcCandidateV1],
) -> EditorialQcReportV1:
    """Aggregate candidates into the versioned report (counts + invariants)."""

    severity: dict[EditorialQcSeverity, int] = {"info": 0, "warning": 0, "critical": 0}
    by_check: dict[EditorialQcCheck, int] = {}
    for candidate in candidates:
        severity[candidate.severity] += 1
        by_check[candidate.check] = by_check.get(candidate.check, 0) + 1
    return EditorialQcReportV1(
        schema_version="editorial-qc-report-v1",
        candidates=tuple(sorted(candidates, key=lambda c: (c.check, _span_of(c), c.detail))),
        counts_by_severity=severity,
        counts_by_check=dict(sorted(by_check.items())),
    )


# --------------------------------------------------- task-47 review-command seam

# Natural mappings only; checks without an obvious correction command carry
# ``None`` and stay plain review items (task 51 wires the cockpit surface).
CHECK_COMMAND_KIND: Final[Mapping[EditorialQcCheck, ReviewCommandKind]] = {
    "duplicated_explanation": "remove_section",
    "awkward_cut": "keep_longer",
    "broll_irrelevance": "insert_broll",
    "long_low_value_segment": "mark_boring",
    "effect_density_excess": "remove_effect",
}


class QcReviewContextItemV1(StrictModel):
    """One candidate projected onto the task-47 command vocabulary."""

    check: EditorialQcCheck
    command_kind: ReviewCommandKind | None
    target_seconds: float | None = None
    severity: EditorialQcSeverity
    needs_human_review: bool
    note: Annotated[str, Field(min_length=1, strict=True)]
    evidence_refs: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)


class QcReviewContextV1(StrictModel):
    """Structured QC payload the review-command parser side can consume."""

    schema_version: Literal["editorial-qc-review-context-v1"]
    items: Annotated[tuple[QcReviewContextItemV1, ...], BeforeValidator(_to_tuple)] = ()


def to_review_context(
    candidates: Sequence[EditorialQcCandidateV1], *, rate: RationalFrameRate
) -> QcReviewContextV1:
    """Project candidates into the structured review-command payload.

    Integration seam (documented, no UI): each item carries the suggested
    task-47 ``command_kind`` where a natural correction exists and the
    player seconds of the candidate span so the cockpit review wiring can
    prefill a ``ReviewCommandDraft`` without parsing prose.
    """

    items = tuple(
        QcReviewContextItemV1(
            check=candidate.check,
            command_kind=CHECK_COMMAND_KIND.get(candidate.check),
            target_seconds=(
                float(candidate.record_span.start_frame / rate.as_fraction)
                if candidate.record_span is not None
                else None
            ),
            severity=candidate.severity,
            needs_human_review=candidate.needs_human_review,
            note=candidate.detail,
            evidence_refs=candidate.evidence_refs,
        )
        for candidate in candidates
    )
    return QcReviewContextV1(schema_version="editorial-qc-review-context-v1", items=items)


__all__ = [
    "ALL_EDITORIAL_CHECKS",
    "CHECK_COMMAND_KIND",
    "DEFAULT_EDITORIAL_QC_THRESHOLDS",
    "DEFAULT_SENSITIVE_PATTERNS",
    "EDITORIAL_QC_ENGINE_VERSION",
    "EditorialQcCandidateV1",
    "EditorialQcCheck",
    "EditorialQcInput",
    "EditorialQcReportV1",
    "EditorialQcSeverity",
    "EditorialQcThresholds",
    "QcReviewContextItemV1",
    "QcReviewContextV1",
    "aggregate_candidates",
    "check_audio_transition_problem",
    "check_awkward_cut",
    "check_broll_irrelevance",
    "check_duplicated_explanation",
    "check_effect_density_excess",
    "check_long_low_value_segment",
    "check_narrative_jump",
    "check_sensitive_private_content",
    "check_subtitle_mismatch",
    "check_unsupported_manual_finalization",
    "run_editorial_qc",
    "to_review_context",
]
