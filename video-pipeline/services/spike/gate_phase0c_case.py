"""Pure Phase-0C case assembly shared by the gate driver and the offline fakes.

Builds, from one frozen fixture manifest only: the base ``EditPlan0C`` (v1),
the deterministic replay-translator inputs (``TranslatorRequest`` plus the
canned strict proposal the Todo-29 replay transport returns). IR loading,
preview projection, and media bindings live in ``gate_phase0c_bindings``.
No ffmpeg, no Resolve, no network — deterministic in every input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
    SubtitleTextSelector0C,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    SourceFrameSpan,
)
from services.fixtures.manifest_phase0c import (
    Phase0CFixtureManifest,
    ReviewCommandSpec0C,
)
from services.review_command.models import (
    AdjustSourceSpanProposal0C,
    CandidateTarget0C,
    Confidence0C,
    CorrectSubtitleProposal0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
    ReviewCommandProposal0C,
    SpanBounds0C,
)
from services.review_command.translator_policy import (
    DEFAULT_TOOLCHAIN_LOCK,
    FrozenTranslatorContract,
    load_frozen_contract,
)
from services.review_command.translator_schema import (
    TranslatorRequest,
    plan_context_from_edit_plan,
)

GATE_PRODUCER: Final = Producer(name="phase0c-gate", version="1")
RATE: Final = RationalFrameRate(num=30, den=1)


def load_case(fixture_id: str, manifest_dir: Path | None = None) -> Phase0CFixtureManifest:
    base = manifest_dir or Path("tests/fixtures/manifests/phase-0c")
    return Phase0CFixtureManifest.model_validate_json(
        (base / f"{fixture_id}.json").read_bytes()
    )


def base_plan(manifest: Phase0CFixtureManifest) -> EditPlan0C:
    items = tuple(
        EditPlanItem0C(
            item_id=item.item_id,
            kind=item.kind,
            source_id=item.source_id,
            span=SourceFrameSpan(
                start_frame=item.span.start_frame,
                end_frame=item.span.end_frame,
                rate=RATE,
            ),
            track_index=item.track_index,
            av_link_id=item.av_link_id,
            subtitle_text=item.subtitle_text,
            locked_fields=item.locked_fields,
        )
        for item in manifest.edit_plan.items
    )
    return EditPlan0C(
        artifact_id=f"edit-plan-{manifest.fixture_id}",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=GATE_PRODUCER,
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(
                source_id=manifest.edit_plan.edit_source.source_id,
                total_frames=manifest.edit_plan.edit_source.total_frames,
            ),
            items=items,
        ),
    )


def _selector(spec: ReviewCommandSpec0C) -> ItemIdSelector0C | SubtitleTextSelector0C:
    if spec.target.kind == "item_id":
        return ItemIdSelector0C(kind="item_id", item_id=spec.target.item_id)
    return SubtitleTextSelector0C(kind="subtitle_text_match", text=spec.target.text)


def replay_proposal(
    manifest: Phase0CFixtureManifest,
    *,
    span_override: tuple[int, int] | None = None,
) -> ReviewCommandProposal0C:
    """The canned model output the replay transport returns for this case.

    Mirrors the frozen Todo-29 replay fixtures: envelope values are derived
    from the manifest only, so the same request hash always replays the same
    proposal. ``span_override`` exists exclusively for the wrong-decision
    fault fake (a decision applying a payload that differs from the frozen
    instruction); the happy path never overrides.
    """

    spec = manifest.command
    proposal_id = f"prop-{manifest.fixture_id}"
    ambiguous = manifest.expected.classification == "ambiguous"
    ambiguity = ProposalAmbiguity0C(
        status="ambiguous" if ambiguous else "clear",
        reasons=("instruction matches multiple plan items",) if ambiguous else (),
    )
    confidence = Confidence0C(num=9, den=10)
    if spec.operation == "remove_segment":
        return RemoveSegmentProposal0C(
            proposal_id=proposal_id,
            command_kind="remove_segment",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=confidence,
            ambiguity=ambiguity,
            evidence=(),
            candidate_targets=tuple(
                CandidateTarget0C(item_id=item_id, evidence=spec.instruction)
                for item_id in manifest.expected.target_candidate_item_ids
            ),
        )
    if spec.operation == "adjust_source_span":
        if spec.new_span is None:
            raise ValueError(f"{manifest.fixture_id}: adjust fixture must declare new_span")
        start, end = (
            span_override
            if span_override is not None
            else (spec.new_span.start_frame, spec.new_span.end_frame)
        )
        return AdjustSourceSpanProposal0C(
            proposal_id=proposal_id,
            command_kind="adjust_source_span",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=confidence,
            ambiguity=ambiguity,
            evidence=(),
            target=_selector(spec),
            new_span=SpanBounds0C(start_frame=start, end_frame=end),
        )
    if spec.new_text is None:
        raise ValueError(f"{manifest.fixture_id}: subtitle fixture must declare new_text")
    return CorrectSubtitleProposal0C(
        proposal_id=proposal_id,
        command_kind="correct_subtitle",
        base_plan_version="v1",
        actor_intent="model",
        sequence=1,
        confidence=confidence,
        ambiguity=ambiguity,
        evidence=(),
        target=_selector(spec),
        new_text=spec.new_text,
        language=spec.language,
    )


def translator_request(
    manifest: Phase0CFixtureManifest,
    plan: EditPlan0C,
    contract: FrozenTranslatorContract | None = None,
) -> TranslatorRequest:
    if contract is None:
        contract = load_frozen_contract(DEFAULT_TOOLCHAIN_LOCK)
    return TranslatorRequest(
        episode_id=manifest.fixture_id,
        policy_profile_id=contract.policy_profile_id,
        schema_version=contract.preview_schema_version,
        instruction=manifest.command.instruction,
        plan_context=plan_context_from_edit_plan(plan),
    )


__all__ = [
    "GATE_PRODUCER",
    "RATE",
    "base_plan",
    "load_case",
    "replay_proposal",
    "translator_request",
]
