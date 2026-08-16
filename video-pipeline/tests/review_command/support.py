from __future__ import annotations

import json
from pathlib import Path

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
    SubtitleTextSelector0C,
    TargetSelector0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.fixtures.manifest_phase0c import Phase0CFixtureManifest
from services.review_command.models import (
    ActorIntent0C,
    AdjustSourceSpanProposal0C,
    ApproveEditorialPlanProposal0C,
    ApproveRemainingProposal0C,
    CandidateTarget0C,
    Confidence0C,
    CorrectSubtitleProposal0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
    ReviewCommandProposal0C,
    SpanBounds0C,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0c")
RATE = RationalFrameRate(num=30, den=1)
TEST_PRODUCER = Producer(name="review-command-test", version="1")


def load_manifest(fixture_id: str) -> Phase0CFixtureManifest:
    return Phase0CFixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def manifest_plan(fixture_id: str) -> EditPlan0C:
    manifest = load_manifest(fixture_id)
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
    body = EditPlanBody0C(
        plan_version="v1",
        edit_source=EditSourceRef0C(
            source_id=manifest.edit_plan.edit_source.source_id,
            total_frames=manifest.edit_plan.edit_source.total_frames,
        ),
        items=items,
    )
    return EditPlan0C(
        artifact_id=f"edit-plan-{manifest.fixture_id}",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=TEST_PRODUCER,
        inputs=(),
        frame_rate=RATE,
        plan=body,
    )


def with_locked_field(plan: EditPlan0C, item_id: str, locked_field: str) -> EditPlan0C:
    items = tuple(
        item.model_copy(update={"locked_fields": (locked_field,)})
        if item.item_id == item_id
        else item
        for item in plan.plan.items
    )
    return plan.model_copy(update={"plan": plan.plan.model_copy(update={"items": items})})


def base_envelope(
    proposal_id: str,
    *,
    actor: str = "model",
    status: str = "clear",
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "base_plan_version": "v1",
        "actor_intent": actor,
        "sequence": 1,
        "confidence": {"num": 9, "den": 10},
        "ambiguity": {"status": status, "reasons": []},
        "evidence": [],
    }


def _target_selector(manifest: Phase0CFixtureManifest) -> TargetSelector0C:
    spec = manifest.command
    if spec.target.kind == "item_id":
        return ItemIdSelector0C(kind="item_id", item_id=spec.target.item_id)
    return SubtitleTextSelector0C(kind="subtitle_text_match", text=spec.target.text)


def _ambiguity(expected: str, override: str | None) -> ProposalAmbiguity0C:
    status = override or ("ambiguous" if expected == "ambiguous" else "clear")
    if status == "ambiguous":
        return ProposalAmbiguity0C(
            status="ambiguous",
            reasons=("instruction matches multiple plan items",),
        )
    return ProposalAmbiguity0C(status="clear")


def _envelope(
    fixture_id: str,
    expected: str,
    override: str | None,
) -> tuple[str, ProposalAmbiguity0C]:
    return f"prop-{fixture_id}", _ambiguity(expected, override)


def fixture_proposal(
    fixture_id: str,
    *,
    authored_status: str | None = None,
) -> ReviewCommandProposal0C:
    manifest = load_manifest(fixture_id)
    spec = manifest.command
    proposal_id, ambiguity = _envelope(
        fixture_id,
        manifest.expected.classification,
        authored_status,
    )
    if spec.operation == "remove_segment":
        return RemoveSegmentProposal0C(
            proposal_id=proposal_id,
            command_kind="remove_segment",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=Confidence0C(num=9, den=10),
            ambiguity=ambiguity,
            evidence=(),
            candidate_targets=tuple(
                CandidateTarget0C(item_id=item_id, evidence=spec.instruction)
                for item_id in manifest.expected.target_candidate_item_ids
            ),
        )
    if spec.operation == "adjust_source_span":
        if spec.new_span is None:
            raise ValueError("adjust fixture must declare new_span")
        return AdjustSourceSpanProposal0C(
            proposal_id=proposal_id,
            command_kind="adjust_source_span",
            base_plan_version="v1",
            actor_intent="model",
            sequence=1,
            confidence=Confidence0C(num=9, den=10),
            ambiguity=ambiguity,
            evidence=(),
            target=_target_selector(manifest),
            new_span=SpanBounds0C(
                start_frame=spec.new_span.start_frame,
                end_frame=spec.new_span.end_frame,
            ),
        )
    if spec.new_text is None:
        raise ValueError("subtitle fixture must declare new_text")
    return CorrectSubtitleProposal0C(
        proposal_id=proposal_id,
        command_kind="correct_subtitle",
        base_plan_version="v1",
        actor_intent="model",
        sequence=1,
        confidence=Confidence0C(num=9, den=10),
        ambiguity=ambiguity,
        evidence=(),
        target=_target_selector(manifest),
        new_text=spec.new_text,
        language=spec.language,
    )


def remove_payload() -> dict[str, object]:
    return base_envelope("prop-remove") | {
        "command_kind": "remove_segment",
        "candidate_targets": [{"item_id": "v2", "evidence": "2番目のセグメントを削除"}],
    }


def adjust_payload(*, start: object, end: object, item_id: str = "v2") -> dict[str, object]:
    return base_envelope("prop-adjust") | {
        "command_kind": "adjust_source_span",
        "target": {"kind": "item_id", "item_id": item_id},
        "new_span": {"start_frame": start, "end_frame": end},
    }


def adjust_proposal(*, start: int, end: int) -> AdjustSourceSpanProposal0C:
    return AdjustSourceSpanProposal0C(
        proposal_id="prop-adjust",
        command_kind="adjust_source_span",
        base_plan_version="v1",
        actor_intent="model",
        sequence=1,
        confidence=Confidence0C(num=9, den=10),
        ambiguity=ProposalAmbiguity0C(status="clear"),
        evidence=(),
        target=ItemIdSelector0C(kind="item_id", item_id="v2"),
        new_span=SpanBounds0C(start_frame=start, end_frame=end),
    )


def approve_remaining_proposal(actor: ActorIntent0C = "operator") -> ReviewCommandProposal0C:
    return ApproveRemainingProposal0C(
        proposal_id="prop-approve-remaining",
        command_kind="approve_remaining",
        base_plan_version="v1",
        actor_intent=actor,
        sequence=2,
        confidence=Confidence0C(num=1, den=1),
        ambiguity=ProposalAmbiguity0C(status="clear"),
        evidence=(),
    )


def approve_editorial_plan_proposal(actor: ActorIntent0C = "operator") -> ReviewCommandProposal0C:
    return ApproveEditorialPlanProposal0C(
        proposal_id="prop-approve-editorial",
        command_kind="approve_editorial_plan",
        base_plan_version="v1",
        actor_intent=actor,
        sequence=3,
        confidence=Confidence0C(num=1, den=1),
        ambiguity=ProposalAmbiguity0C(status="clear"),
        evidence=(),
    )


def approval_payload(kind: str, *, actor: str) -> str:
    payload = base_envelope(f"prop-approve-{kind}", actor=actor)
    payload["command_kind"] = f"approve_{kind}"
    return json.dumps(payload)
