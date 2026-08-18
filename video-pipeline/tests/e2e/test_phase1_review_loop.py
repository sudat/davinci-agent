"""Todo 45 e2e: the Phase-1 review loop over the p1-ref-05-review-mix fixture.

One deterministic chain run reaches PREVIEW_READY with a review bundle; the
ordered tests then drive the frozen declared correction sequence: a CLEAR
correction applies once (new plan/IR/preview, complete event lineage, base
advanced, idempotent replay), an AMBIGUOUS instruction classifies ambiguous
and never mutates the plan, stale-base proposals are typed rejections, and
schema-gap instructions are refused with typed reasons. Metrics are event
derived and never carry KPI claims from fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from services.cli.bundle import load_bundle, rehash_bundle_targets
from services.cli.project import plan_sha256
from services.cli.review_apply import apply_proposal
from services.cli.review_instructions import canonical_instruction
from services.cli.review_replay import (
    PlanView,
    propose_review_command,
)
from services.config.models import ResolvedConfig
from services.editorial.correction_metrics import (
    CorrectionMetricsArtifact,
    kpi_claim_permitted,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.review_command.store import load_events, load_head

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from tests.e2e.conftest import ReviewRig

CLEAR_INSTRUCTION = "字幕 st1 を「とても美味しいですね。」に修正してください。"
AMBIGUOUS_INSTRUCTION = "「はい、そうです。」という字幕を「はい、違います。」に修正してください。"
REMOVE_INSTRUCTION = "セグメント s2 を削除してください。"
GAP_INSTRUCTION = "全部消して、見せ場だけ残していい感じにしてください"


def _propose(rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest, instruction: str, name: str):
    bundle = load_bundle(rig.bundle_file)
    head = load_head(
        rig.bundle_file.parent / bundle.events_log, rig.bundle_file.parent / bundle.store_dir
    )
    return propose_review_command(
        manifest,
        instruction,
        PlanView(plan=head.plan, version=f"v{head.version}", plan_hash=plan_sha256(head.plan)),
        ResolvedConfig.model_validate_json(rig.policy_file.read_bytes()),
        policy_sha=sha256_file(rig.policy_file),
        translator_sha=sha256_file(Path("config/gates/phase-0c-v1.json")),
    ), rig.root / f"{name}.json"


def _write(rig: ReviewRig, outcome, path: Path) -> Path:
    atomic_write(path, canonical_model_bytes(outcome))
    return path


def _expected_rows(manifest: Phase1TechnicalFixtureManifest) -> list[Mapping[str, object]]:
    expected = manifest.expected
    assert isinstance(expected, dict)
    rows = expected["plan_items"]
    assert isinstance(rows, list)
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("kind"), str)]


def test_00_run_reaches_preview_ready_matching_expected_tables(
    rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest
) -> None:
    bundle = load_bundle(rig.bundle_file)
    rehash_bundle_targets(bundle, rig.bundle_file)
    assert bundle.current.plan_version == "v1"
    assert (rig.bundle_file.parent / "preview-v1" / "preview.mp4").is_file()

    def norm(item: dict[str, object]) -> tuple[object, ...]:
        span = item["span"]
        assert isinstance(span, dict)
        return (
            item["kind"],
            span["start_frame"],
            span["end_frame"],
            item["track_index"],
            item["av_link_id"],
            item.get("subtitle_text"),
        )

    plan = json.loads(
        (rig.bundle_file.parent / bundle.store_dir / "plan-v1.json").read_bytes()
    )
    items = plan["plan"]["items"]
    assert isinstance(items, list)
    got = [norm(item) for item in items if isinstance(item, dict)]
    expected = [
        (
            row["kind"],
            row_dict(row)["start_frame"],
            row_dict(row)["end_frame"],
            row["track_index"],
            row["av_link_id"],
            row.get("subtitle_text"),
        )
        for row in _expected_rows(manifest)
    ]
    assert got == expected
    # video items carry the segment id (the declared review vocabulary); the
    # abstract table names them v{N} — same structure, review-friendly ids.
    video_ids = [item["item_id"] for item in items if item.get("kind") == "video"]
    assert video_ids == ["s1", "s2", "s3", "s4"]


def row_dict(row: Mapping[str, object]) -> Mapping[str, object]:
    span = row["span"]
    assert isinstance(span, dict)
    return span


def test_10_ambiguous_instruction_classifies_and_never_mutates(
    rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest
) -> None:
    assert canonical_instruction(manifest.review_commands[3]) == AMBIGUOUS_INSTRUCTION
    outcome, _ = _propose(rig, manifest, AMBIGUOUS_INSTRUCTION, "prop-amb")
    assert outcome.status == "proposal"
    assert outcome.classification == "ambiguous"
    assert tuple(outcome.candidate_item_ids) == ("st3", "st4")
    proposal_file = _write(rig, outcome, rig.root / "prop-amb.json")
    plan_before = (
        rig.bundle_file.parent / "review-store" / "plan-v1.json"
    ).read_bytes()
    result, code = apply_proposal(proposal_file, rig.bundle_file, rig.root / "apply-amb")
    assert code == 0
    assert result.applied is False
    assert result.reason_code == "ambiguous"
    assert result.classification == "ambiguous"
    assert (
        rig.bundle_file.parent / "review-store" / "plan-v1.json"
    ).read_bytes() == plan_before
    assert not (rig.bundle_file.parent / "preview-v2").exists()


def test_11_schema_gap_instruction_is_refused_with_typed_reason(
    rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest
) -> None:
    outcome, _ = _propose(rig, manifest, GAP_INSTRUCTION, "prop-gap")
    assert outcome.status == "error"
    assert outcome.error_code == "replay_mismatch"
    assert outcome.proposal_json is None
    proposal_file = _write(rig, outcome, rig.root / "prop-gap.json")
    result, code = apply_proposal(proposal_file, rig.bundle_file, rig.root / "apply-gap")
    assert code == 1
    assert result.applied is False
    assert result.reason_code == "schema_gap"
    assert "replay_mismatch" in str(result.reason_detail)


def test_12_propose_fresh_v1_candidate_for_later_staleness(
    rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest
) -> None:
    assert canonical_instruction(manifest.review_commands[0]) == REMOVE_INSTRUCTION
    outcome, _ = _propose(rig, manifest, REMOVE_INSTRUCTION, "prop-remove")
    assert outcome.status == "proposal"
    assert outcome.classification == "clear"
    _write(rig, outcome, rig.root / "prop-remove.json")


def test_20_clear_correction_applies_regenerates_and_advances_base(
    rig: ReviewRig, manifest: Phase1TechnicalFixtureManifest
) -> None:
    assert canonical_instruction(manifest.review_commands[2]) == CLEAR_INSTRUCTION
    outcome, _ = _propose(rig, manifest, CLEAR_INSTRUCTION, "prop-clear")
    assert outcome.classification == "clear"
    assert outcome.base_plan_hash == load_bundle(rig.bundle_file).current.plan_sha256
    proposal_file = _write(rig, outcome, rig.root / "prop-clear.json")
    before = load_bundle(rig.bundle_file)
    result, code = apply_proposal(proposal_file, rig.bundle_file, rig.root / "apply-clear")
    assert code == 0
    assert result.applied is True
    assert result.idempotent is False
    assert result.version == 2

    after = load_bundle(rig.bundle_file)
    assert after.current.plan_version == "v2"
    assert after.current.plan_sha256 != before.current.plan_sha256
    assert after.current.preview_dir == "preview-v2"
    rehash_bundle_targets(after, rig.bundle_file)
    preview = rig.bundle_file.parent / "preview-v2" / "preview.mp4"
    assert sha256_file(preview) == after.current.preview_sha256
    plan_v2 = json.loads(
        (rig.bundle_file.parent / "review-store" / "plan-v2.json").read_bytes()
    )
    st1 = next(i for i in plan_v2["plan"]["items"] if i["item_id"] == "st1")
    assert st1["subtitle_text"] == "とても美味しいですね。"

    events = load_events(rig.bundle_file.parent / "review-store" / "events.jsonl")
    kinds = [event.kind for event in events]
    assert kinds.count("decision_applied") == 1
    assert kinds.count("command_deferred") == 1
    applied = next(event for event in events if event.kind == "decision_applied")
    assert applied.event_id == result.event_id
    assert applied.result_plan_version == "v2"
    assert tuple(after.applied_event_ids) == (applied.event_id,)


def test_21_stale_base_proposal_is_a_typed_rejection(rig: ReviewRig) -> None:
    result, code = apply_proposal(
        rig.root / "prop-remove.json", rig.bundle_file, rig.root / "apply-stale"
    )
    assert code == 1
    assert result.applied is False
    assert result.reason_code == "stale_base"
    assert load_bundle(rig.bundle_file).current.plan_version == "v2"


def test_22_reapplying_the_same_proposal_is_idempotent(rig: ReviewRig) -> None:
    events_before = load_events(rig.bundle_file.parent / "review-store" / "events.jsonl")
    result, code = apply_proposal(
        rig.root / "prop-clear.json", rig.bundle_file, rig.root / "apply-again"
    )
    assert code == 0
    assert result.applied is True
    assert result.idempotent is True
    events_after = load_events(rig.bundle_file.parent / "review-store" / "events.jsonl")
    assert len(events_after) == len(events_before)
    bundle = load_bundle(rig.bundle_file)
    assert bundle.current.plan_version == "v2"
    assert len(bundle.applied_event_ids) == 1


def test_23_metrics_are_event_derived_and_never_kpi_claims(rig: ReviewRig) -> None:
    metrics = json.loads(
        (rig.root / "apply-again" / "correction-metrics.json").read_bytes()
    )
    assert metrics["counts_by_classification"] == {
        "clear": 1,
        "ambiguous": 1,
        "conflict": 0,
        "unclassified": 0,
    }
    assert metrics["total_applied"] == 1
    assert metrics["total_deferred"] == 1
    assert metrics["structured_ratio_num"] == 1
    assert metrics["structured_ratio_den"] == 2
    assert metrics["fixture_only"] is True
    kinds = metrics["correction_kinds"]
    assert isinstance(kinds, list)
    assert [(row["kind"], row["count"]) for row in kinds] == [("correct_subtitle", 2)]
    artifact = CorrectionMetricsArtifact.model_validate_json(
        (rig.root / "apply-again" / "correction-metrics.json").read_bytes()
    )
    assert kpi_claim_permitted(artifact) is False
    events = load_events(rig.bundle_file.parent / "review-store" / "events.jsonl")
    assert tuple(artifact.source_event_ids) == tuple(event.event_id for event in events)
