"""Bound 全編へ authorization regressions (P1-1/P1-2, store level).

The authorization is bound server-side to its target (head version +
plan sha, adopted-policy sha, viewed sample + content sha): a moved
target, a tampered sample, or a legacy unbound row all fail closed.
Re-sends dedupe against the whole journal (zero new rows) and a moved
target under the same operation id is a typed 409 — never a silent
re-authorize, never an overwrite of a newer judgment.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from pathlib import Path

import pytest

from services.compile.sample_projection import project_sample_ir
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemKind0C,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)
from services.contracts.timeline_ir import TimelineIr0C
from services.episode_cockpit.consultation_selection_budget import (
    attempt_for,
    reserve_preview,
    settle_preview,
)
from services.episode_cockpit.consultation_store import (
    ConsultationJudgmentV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_consultation,
    append_full_authorization_once,
    append_judgment,
    append_proposal_set,
    canonical_policy_sha256,
    consultation_adoption_open,
    full_render_authorized,
    latest_adopted_policy,
    load_judgments,
    now_stamp,
)
from services.episode_cockpit.episode_files import consultation_flow_routing
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_complete import write_published_manifest
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SampleManifestV1,
    SampleRequestIdentityV1,
    derive_sample_id,
    sample_dir,
)
from services.episode_cockpit.sample_journal import request_sample
from services.review_command.commit import commit_command
from services.review_command.store import (
    OperatorDecision0C,
    initialize_store,
    load_head,
)
from services.validate.edit_commit_schema import tuplize
from tests.review_command.support import fixture_proposal, manifest_plan

_DETAILS = {
    "audience_message": "始めて見る人に分かる導入",
    "structure": "導入→本編→締め",
    "duration_estimate": "約4分",
    "candidate_scenes": ["opening", "demo"],
    "subtitle_policy": "短めの字幕",
    "audio_policy": "BGM小さめ",
    "tempo_policy": "前半テンポ重視",
    "reference_mapping": "参考1の構成を踏襲",
    "unused_reasons": "未使用素材はなし",
    "unconfirmed": ["尺の希望"],
}

VIDEO = b"viewed-sample-bytes"

RATE = RationalFrameRate(num=30, den=1)


def _seed_plan_av() -> EditPlan0C:
    """Video+audio plan: its store IR projects under the 1V+1A contract.

    Guard-only suites needing a bound authorization use this (a
    video-only seed plan has no audio track, so no sample can verify
    against it — exactly like production, where the sample render would
    stop there first).
    """

    def av(  # noqa: PLR0913, PLR0917 (test-plan row: one field per plan slot)
        item_id: str, kind: ItemKind0C, start: int, end: int, track: int, link: str
    ) -> EditPlanItem0C:
        return EditPlanItem0C(
            item_id=item_id,
            kind=kind,
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=track,
            av_link_id=link,
        )

    return EditPlan0C(
        artifact_id="edit-plan-review-ep-seed-av",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="phase1-review-plane", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=60),
            items=(
                av("v1", "video", 0, 30, 1, "av1"),
                av("v2", "video", 30, 60, 1, "av2"),
                av("a1", "audio", 0, 30, 2, "av1"),
                av("a2", "audio", 30, 60, 2, "av2"),
            ),
        ),
    )


def _seed_policy_episode(episode_dir: Path) -> None:
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    initialize_store(
        manifest_plan("p0c-remove-clear"),
        episode_dir / "review" / "events.jsonl",
        store_dir,
    )
    _seed_consultation(episode_dir)


def _seed_consultation(episode_dir: Path) -> None:
    append_consultation(
        episode_dir,
        ConsultationRecordV1(
            consultation_id="c1", created_at=now_stamp(), message="短くしたい"
        ),
    )
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id="c1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(_DETAILS),
                ),
            ),
        ),
    )
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j1",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": "prop-1",
                "decision": "adopt",
                "scope": {"composition": True, "appearance": False, "audio": False},
                "note": "構成だけ採用",
            }
        ),
    )


_seed_ids = itertools.count(1)


def seed_viewed_sample(
    episode_dir: Path,
    consultation_id: str = "c1",
    *,
    windows: tuple[tuple[int, int], ...] = ((0, 30),),
    operation_id: str | None = None,
    video: bytes = VIDEO,
) -> SampleManifestV1:
    """Store a FULLY-BOUND published sample viewing the current head + policy.

    Fully-bound = reserve journal + budget reserve/settle + the head IR
    projected over the requested windows + real preview bytes, so the
    shared verify_recovery boundary covers it exactly like a rendered
    sample. ``operation_id`` defaults unique per call (the same op with
    a different identity would be a typed 409 by contract). The head IR
    must be projectable under the sample renderer's 1V+1A contract —
    guard-only suites with synthetic video-only episodes pass a plan
    carrying audio (see the callers), never a hand-written IR file.
    """

    seq = next(_seed_ids)
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    policy = latest_adopted_policy(episode_dir)
    assert policy is not None
    head_entry = head.index.versions[str(head.version)]
    base_version = f"v{head.version}"
    ir_path = episode_dir / "review" / "store" / f"ir-{base_version}.json"
    ir_bytes = ir_path.read_bytes()
    full_ir = TimelineIr0C.model_validate(tuplize(json.loads(ir_bytes)))
    spans = [RecordFrameSpan(start_frame=w[0], end_frame=w[1]) for w in windows]
    identity = SampleRequestIdentityV1.model_validate(
        {
            "episode_id": "ep-auth",
            "consultation_id": consultation_id,
            "judgment_id": policy.judgment_id,
            "base_version": base_version,
            "base_plan_sha256": head_entry.plan_sha256,
            "policy_sha256": canonical_policy_sha256(policy),
            "output_id": "landscape",
            "windows": [{"start_frame": w[0], "end_frame": w[1]} for w in windows],
            "operation_id": operation_id or f"op-sample-{seq}",
            "full_ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
        }
    )
    request = request_sample(episode_dir, identity)
    assert request["state"] == "reserved"
    budget = attempt_for(seq, consultation_id, policy.judgment_id)
    reserve_preview(episode_dir, budget, 1.0)
    settle_preview(
        episode_dir,
        budget,
        preview_seconds=1.0,
        wall_elapsed=0.05,
        result="succeeded",
    )
    target = sample_dir(episode_dir, derive_sample_id(identity))
    target.mkdir(parents=True, exist_ok=True)
    (target / SAMPLE_PREVIEW_NAME).write_bytes(video)
    return write_published_manifest(
        target,
        identity,
        sample_ir=project_sample_ir(full_ir, spans),
        total_seconds=1.0,
        content_sha256=hashlib.sha256(video).hexdigest(),
        sample_attempt_id=request["sample_attempt_id"],
        budget_entry_id=budget.attempt_id,
        budget_reservation_sequence=budget.reservation_sequence,
        run_id=f"run-auth-{seq:04d}",
        wall_seconds_used=0.05,
    )


def _seed_second_consultation(episode_dir: Path) -> str:
    """Adopt prop-1 under c2; returns its judgment_id."""

    append_consultation(
        episode_dir,
        ConsultationRecordV1(
            consultation_id="c2", created_at=now_stamp(), message="音を整えたい"
        ),
    )
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id="c2",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(_DETAILS),
                ),
            ),
        ),
    )
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-c2",
                "created_at": now_stamp(),
                "consultation_id": "c2",
                "proposal_id": "prop-1",
                "decision": "adopt",
                "scope": {"composition": True, "appearance": False, "audio": False},
                "note": "c2の構成だけ採用",
            }
        ),
    )
    return "j-c2"


def authorize_bound(
    episode_dir: Path, operation_id: str = "op-auth-1", *, sample_id: str
) -> ConsultationJudgmentV1:
    judgment, appended = append_full_authorization_once(
        episode_dir,
        consultation_id="c1",
        sample_id=sample_id,
        scope=ConsultationScope(),
        note="全編へ",
        operation_id=operation_id,
    )
    assert appended is True
    return judgment


def _judgment_lines(episode_dir: Path) -> int:
    return len((episode_dir / "consultation" / "judgments.jsonl").read_bytes().splitlines())


def test_bound_authorization_holds_when_state_current(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True


def test_head_change_closes_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    commit_command(
        fixture_proposal("p0c-span-clear"),
        OperatorDecision0C(decision_id="dec-apply-1", actor_intent="operator"),
        episode_dir / "review" / "events.jsonl",
        episode_dir / "review" / "store",
    )
    assert full_render_authorized(episode_dir) is False


def test_policy_change_closes_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    changed = dict(_DETAILS)
    changed["structure"] = "本編→導入→締め"
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id="c1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(changed),
                ),
            ),
        ),
    )
    assert full_render_authorized(episode_dir) is False


def test_sample_tamper_closes_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    path = sample_dir(episode_dir, manifest.sample_id) / SAMPLE_MANIFEST_NAME
    raw = json.loads(path.read_bytes())
    raw["content_sha256"] = "0" * 64
    path.write_bytes(json.dumps(raw, sort_keys=True).encode())
    assert full_render_authorized(episode_dir) is False


def test_sample_missing_closes_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    (sample_dir(episode_dir, manifest.sample_id) / SAMPLE_PREVIEW_NAME).unlink()
    assert full_render_authorized(episode_dir) is False


def test_legacy_unbound_row_authorizes_nothing(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-legacy",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": None,
                "decision": "full_authorized",
                "scope": {},
                "note": "全編へ",
            }
        ),
    )
    assert full_render_authorized(episode_dir) is False
    assert consultation_adoption_open(episode_dir) is True
    _, stop = consultation_flow_routing(episode_dir, "cmd-nl-legacy")
    assert stop is not None


def test_newer_judgment_closes_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j2",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": "prop-1",
                "decision": "reject",
                "scope": {"composition": True, "appearance": False, "audio": False},
                "note": "やっぱり見送り",
            }
        ),
    )
    assert full_render_authorized(episode_dir) is False


def test_resend_same_binding_returns_existing_with_zero_rows(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    first = authorize_bound(episode_dir, operation_id="op-resend", sample_id=manifest.sample_id)
    lines_before = _judgment_lines(episode_dir)
    second, appended = append_full_authorization_once(
        episode_dir,
        consultation_id="c1",
        sample_id=manifest.sample_id,
        scope=ConsultationScope(),
        note="全編へ",
        operation_id="op-resend",
    )
    assert appended is False
    assert second.judgment_id == first.judgment_id
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is True


def test_resend_moved_target_is_typed_409(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, operation_id="op-moved", sample_id=manifest.sample_id)
    lines_before = _judgment_lines(episode_dir)
    changed = dict(_DETAILS)
    changed["tempo_policy"] = "後半テンポ重視"
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id="c1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(changed),
                ),
            ),
        ),
    )
    with pytest.raises(CockpitConflictError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id=manifest.sample_id,
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-moved",
        )
    assert exc_info.value.code == "consultation-judgment-conflict"
    assert _judgment_lines(episode_dir) == lines_before


def test_delayed_resend_preserves_newer_judgment(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    first = authorize_bound(
        episode_dir, operation_id="op-delayed", sample_id=manifest.sample_id
    )
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-newer",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": "prop-1",
                "decision": "reject",
                "scope": {"composition": True, "appearance": False, "audio": False},
                "note": "やっぱり見送り",
                "operation_id": "op-newer",
            }
        ),
    )
    lines_before = _judgment_lines(episode_dir)
    second, appended = append_full_authorization_once(
        episode_dir,
        consultation_id="c1",
        sample_id=manifest.sample_id,
        scope=ConsultationScope(),
        note="全編へ",
        operation_id="op-delayed",
    )
    assert appended is False
    assert second.judgment_id == first.judgment_id
    assert _judgment_lines(episode_dir) == lines_before
    assert load_judgments(episode_dir)[-1].judgment_id == "j-newer"
    assert full_render_authorized(episode_dir) is False


def test_other_sample_does_not_transfer_authorization(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    other = seed_viewed_sample(
        episode_dir, windows=((60, 90),), video=b"other-sample-bytes"
    )
    assert other.sample_id != manifest.sample_id
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True
    shutil.rmtree(sample_dir(episode_dir, manifest.sample_id))
    assert full_render_authorized(episode_dir) is False


def test_explicit_sample_a_binds_a_not_newer_b(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    sample_a = seed_viewed_sample(episode_dir, windows=((0, 30),))
    sample_b = seed_viewed_sample(episode_dir, windows=((60, 90),))
    assert sample_a.sample_id != sample_b.sample_id
    judgment = authorize_bound(
        episode_dir, operation_id="op-explicit-a", sample_id=sample_a.sample_id
    )
    assert judgment.auth_sample_id == sample_a.sample_id
    assert full_render_authorized(episode_dir) is True


def test_unspecified_sample_is_422_with_journal_unchanged(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    lines_before = _judgment_lines(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id=None,
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-no-sample",
        )
    assert exc_info.value.code == "consultation-authorization-no-sample"
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is False


def test_nonexistent_sample_is_422_with_journal_unchanged(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    lines_before = _judgment_lines(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id="sample-0000000000000000",
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-ghost-sample",
        )
    assert exc_info.value.code == "consultation-authorization-no-sample"
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is False


def test_other_consultation_sample_is_422_with_journal_unchanged(
    tmp_path: Path,
) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    _seed_second_consultation(episode_dir)
    foreign = seed_viewed_sample(episode_dir, consultation_id="c2")
    lines_before = _judgment_lines(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id=foreign.sample_id,
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-foreign-sample",
        )
    assert exc_info.value.code == "consultation-authorization-no-sample"
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is False


def test_tampered_preview_bytes_refuses_creation(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    (sample_dir(episode_dir, manifest.sample_id) / SAMPLE_PREVIEW_NAME).write_bytes(
        b"tampered-bytes-only-manifest-unchanged"
    )
    lines_before = _judgment_lines(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id=manifest.sample_id,
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-tampered-create",
        )
    assert exc_info.value.code == "consultation-authorization-no-sample"
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is False


def test_tampered_preview_bytes_closes_guard(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(
        episode_dir, operation_id="op-tampered-guard", sample_id=manifest.sample_id
    )
    assert full_render_authorized(episode_dir) is True
    (sample_dir(episode_dir, manifest.sample_id) / SAMPLE_PREVIEW_NAME).write_bytes(
        b"tampered-bytes-only-manifest-unchanged"
    )
    assert full_render_authorized(episode_dir) is False


def test_reject_blocks_new_authorization_no_archaeology(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep"
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j-reject-blocks",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": "prop-1",
                "decision": "reject",
                "scope": {"composition": True, "appearance": False, "audio": False},
                "note": "やっぱり見送り",
                "operation_id": "op-reject-blocks",
            }
        ),
    )
    lines_before = _judgment_lines(episode_dir)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        append_full_authorization_once(
            episode_dir,
            consultation_id="c1",
            sample_id=manifest.sample_id,
            scope=ConsultationScope(),
            note="全編へ",
            operation_id="op-after-reject",
        )
    assert exc_info.value.code == "consultation-authorization-no-policy"
    assert _judgment_lines(episode_dir) == lines_before
    assert full_render_authorized(episode_dir) is False
