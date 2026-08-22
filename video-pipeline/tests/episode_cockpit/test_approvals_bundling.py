"""Task 49: approval bundling, restart recovery, error cards, publish read path.

PRD 13.4 UX SLO: compatible approvals bundle into at most TWO normal
blocking sessions (editorial/presentation + final/publication);
privacy/rights/manual-freeze exceptions become EXTRA explained bundles,
never silently merged. Restart recovery rebuilds the operator view from
persisted StateStore + episode files only. Error cards state what
failed / what output is affected / retry safety / fallback / next
action — raw stack traces never reach the operator surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.approvals import (
    ApprovalBundles,
    PendingApproval,
    bundle_approvals,
    count_blocking_sessions,
)
from services.episode_cockpit.error_cards import (
    ErrorContext,
    build_error_card,
)
from services.episode_cockpit.recovery import RecoveryError, resume_state
from services.foundation_io import canonical_model_bytes
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore
from services.publish.idempotency import UploadLedger
from services.publish.models import PublishPackageV1, build_package

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {
        "state_store": tmp_path / "state.db",
        "episodes_root": tmp_path / "jobs",
    }


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> dict[str, object]:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "travel vlog, calm pacing"},
    )
    assert response.status_code == 200
    return response.json()


def _pending(record_id: str, purpose: str, target_hash: str) -> PendingApproval:
    return PendingApproval(
        record_id=record_id, purpose=purpose, target_hash=target_hash  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# (a) mixed compatible pending approvals -> exactly 2 normal bundles
# ---------------------------------------------------------------------------


def test_bundle_mixed_pending_into_two_normal_sessions() -> None:
    given_pending = [
        _pending("opr-00000001", "editorial", "a" * 64),
        _pending("opr-00000002", "presentation", "b" * 64),
        _pending("opr-00000003", "final", "c" * 64),
        _pending("opr-00000004", "publication", "d" * 64),
    ]

    bundles = bundle_approvals(given_pending)

    assert [bundle.session_key for bundle in bundles.bundles] == [
        "editorial-presentation",
        "final-publication",
    ]
    assert all(bundle.kind == "normal" for bundle in bundles.bundles)
    first, second = bundles.bundles
    assert [item.record_id for item in first.items] == ["opr-00000001", "opr-00000002"]
    assert [item.record_id for item in second.items] == ["opr-00000003", "opr-00000004"]


def test_bundle_only_emits_nonempty_sessions() -> None:
    given_pending = [_pending("opr-00000001", "editorial", "a" * 64)]

    bundles = bundle_approvals(given_pending)

    assert [bundle.session_key for bundle in bundles.bundles] == ["editorial-presentation"]
    assert count_blocking_sessions(bundles) == 1


# ---------------------------------------------------------------------------
# (b) the <=2 metric
# ---------------------------------------------------------------------------


def test_count_blocking_sessions_is_two_for_mixed_normal_pending() -> None:
    given_pending = [
        _pending("opr-00000001", "editorial", "a" * 64),
        _pending("opr-00000002", "presentation", "b" * 64),
        _pending("opr-00000003", "final", "c" * 64),
        _pending("opr-00000004", "publication", "d" * 64),
    ]

    assert count_blocking_sessions(bundle_approvals(given_pending)) == 2


def test_count_blocking_sessions_is_zero_without_pending() -> None:
    assert count_blocking_sessions(bundle_approvals([])) == 0


# ---------------------------------------------------------------------------
# (c) privacy/rights/manual-freeze exceptions: extra explained bundles
# ---------------------------------------------------------------------------


def test_privacy_exception_adds_explained_third_session() -> None:
    given_pending = [
        _pending("opr-00000001", "editorial", "a" * 64),
        _pending("opr-00000002", "presentation", "b" * 64),
        _pending("opr-00000003", "final", "c" * 64),
        _pending("opr-00000004", "publication", "d" * 64),
        _pending("opr-00000005", "privacy", "e" * 64),
        _pending("opr-00000006", "privacy", "f" * 64),
    ]

    bundles = bundle_approvals(given_pending)

    assert count_blocking_sessions(bundles) == 3
    normal_keys = [b.session_key for b in bundles.bundles if b.kind == "normal"]
    assert normal_keys == ["editorial-presentation", "final-publication"]
    exceptions = [b for b in bundles.bundles if b.kind == "exception"]
    assert [b.session_key for b in exceptions] == ["privacy"]
    privacy = exceptions[0]
    assert [item.record_id for item in privacy.items] == ["opr-00000005", "opr-00000006"]
    assert privacy.explanation
    # never silently merged: the privacy items appear in no normal bundle
    for bundle in bundles.bundles:
        if bundle.kind == "normal":
            assert "opr-00000005" not in [item.record_id for item in bundle.items]


def test_each_exception_class_gets_its_own_explained_bundle() -> None:
    given_pending = [
        _pending("opr-00000001", "rights", "a" * 64),
        _pending("opr-00000002", "manual_freeze", "b" * 64),
        _pending("opr-00000003", "privacy", "c" * 64),
    ]

    bundles = bundle_approvals(given_pending)

    assert [b.session_key for b in bundles.bundles] == ["privacy", "rights", "manual_freeze"]
    assert all(b.kind == "exception" for b in bundles.bundles)
    assert all(b.explanation for b in bundles.bundles)
    assert count_blocking_sessions(bundles) == 3


def test_bundle_approvals_rejects_malformed_pending_input() -> None:
    with pytest.raises(ValidationError):
        bundle_approvals([{"record_id": "opr-1", "purpose": "not-a-purpose", "target_hash": "z"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# (d) recovery: view rebuilt identically from persisted state alone
# ---------------------------------------------------------------------------


EPISODE_ID = "ep-recover01"


def _seed_persisted_episode(workspace: dict[str, Path]) -> None:
    with StateStore.open(workspace["state_store"]) as store:
        store.create_job(job_id=EPISODE_ID, episode_id=EPISODE_ID, current_stage="ingest")
        store.record_stage_run(
            StageRunRow(
                job_id=EPISODE_ID,
                stage_name="ingest",
                idempotency_key="ingest-run-1",
                input_artifact_hashes=(),
                adopted_artifact_hash=None,
                status="succeeded",
            )
        )
    approvals = OperationRecordStore(
        workspace["episodes_root"] / EPISODE_ID / "approvals" / "records.jsonl"
    )
    approvals.append(
        record_fixture_operation(
            purpose="editorial",
            target_bundle_hash="a" * 64,
            decision="approve",
            actor_id="operator-1",
        )
    )
    approvals.append(
        record_fixture_operation(
            purpose="editorial",
            target_bundle_hash="a" * 64,
            decision="reject",
            actor_id="operator-1",
        )
    )
    approvals.append(
        record_fixture_operation(
            purpose="privacy",
            target_bundle_hash="b" * 64,
            decision="approve",
            actor_id="operator-1",
        )
    )
    chat = workspace["episodes_root"] / EPISODE_ID / "review-chat.jsonl"
    chat.parent.mkdir(parents=True, exist_ok=True)
    chat.write_bytes(
        b'{"schema_version":"cockpit-review-chat-v1","sequence":1,"text":"first note"}\n'
        b'{"schema_version":"cockpit-review-chat-v1","sequence":2,"text":"second note"}\n'
    )


def test_resume_state_rebuilds_identically_after_restart(
    workspace: dict[str, Path],
) -> None:
    _seed_persisted_episode(workspace)

    before_restart = resume_state(
        workspace["state_store"], workspace["episodes_root"]
    )
    after_restart = resume_state(
        workspace["state_store"], workspace["episodes_root"]
    )  # fresh call: no in-memory state anywhere

    assert before_restart == after_restart
    assert before_restart.model_dump() == after_restart.model_dump()
    assert len(after_restart.episodes) == 1
    episode = after_restart.episodes[0]
    assert episode.episode_id == EPISODE_ID
    assert episode.status == "CREATED"
    assert episode.current_stage == "ingest"
    assert [run.stage_name for run in episode.stage_runs] == ["ingest"]
    assert episode.stage_runs[0].status == "succeeded"
    # acceptance is a file/ledger fact: the superseding reject survives,
    # and the independent privacy approval survives as its own fact.
    assert len(episode.approvals) == 2
    editorial = next(f for f in episode.approvals if f.purpose == "editorial")
    assert editorial.decision == "reject"
    assert episode.review_messages == 2


def test_resume_state_absent_store_is_empty_view(workspace: dict[str, Path]) -> None:
    view = resume_state(workspace["state_store"], workspace["episodes_root"])

    assert view.episodes == ()


# ---------------------------------------------------------------------------
# (e) unreadable state -> typed error with recovery hint
# ---------------------------------------------------------------------------


def test_resume_state_unreadable_store_is_typed_error_with_hint(
    workspace: dict[str, Path],
) -> None:
    workspace["state_store"].parent.mkdir(parents=True, exist_ok=True)
    workspace["state_store"].write_bytes(b"this is not a sqlite database")

    with pytest.raises(RecoveryError) as raised:
        resume_state(workspace["state_store"], workspace["episodes_root"])

    assert raised.value.code == "state-store-unreadable"
    assert raised.value.hint


# ---------------------------------------------------------------------------
# (f) error cards: structured fields, no stack trace on the surface
# ---------------------------------------------------------------------------


def _card_context(**overrides: object) -> ErrorContext:
    payload: dict[str, object] = {
        "failed_stage": "resolve-build",
        "affected_output": "final-render.mp4",
        "fallback_available": False,
        "debug_log_path": "jobs/ep-x/logs/resolve-build.log",
    }
    payload.update(overrides)
    return ErrorContext.model_validate(payload)


def test_error_card_hides_stack_trace_and_populates_fields() -> None:
    given_exception = ValueError(
        "Traceback (most recent call last):\n"
        '  File "/secret/path/module.py", line 42, in boom\n'
        "    raise ValueError('inner detail')\n"
        "ValueError: inner detail"
    )

    card = build_error_card(given_exception, _card_context())

    surface = card.model_dump_json()
    assert "Traceback" not in surface
    assert "/secret/path" not in surface
    assert "inner detail" not in surface
    assert card.code == "ValueError"
    assert card.failed_stage == "resolve-build"
    assert card.affected_output == "final-render.mp4"
    assert card.fallback_available is False
    assert card.next_action_hint
    assert card.debug_ref == "jobs/ep-x/logs/resolve-build.log"


@pytest.mark.parametrize(
    ("source", "expected_retry_safe"),
    [
        (TimeoutError("timed out"), True),
        (ConnectionError("refused"), True),
        (ValidationError.from_exception_data("X", []), False),
        (RuntimeError("mystery failure"), None),
        ("stage-timeout", True),
        ("schema-invalid", False),
        ("unknown-code", None),
    ],
)
def test_error_card_retry_safety_classification(
    source: BaseException | str,
    expected_retry_safe: bool | None,  # noqa: FBT001 (parametrized fixture value)
) -> None:
    card = build_error_card(source, _card_context())

    assert card.retry_safe == expected_retry_safe


def test_error_card_hint_mentions_fallback_when_available() -> None:
    card = build_error_card("stage-timeout", _card_context(fallback_available=True))

    assert card.fallback_available is True
    assert card.next_action_hint


# ---------------------------------------------------------------------------
# (g) publish-status read path over the real publish models
# ---------------------------------------------------------------------------


def test_publish_status_absent_is_structured_not_available(
    client: TestClient, source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)

    response = client.get(f"/episodes/{created['episode_id']!s}/publish-status")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"]


def test_publish_status_with_synthetic_package_and_ledger_is_populated(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])
    package: PublishPackageV1 = build_package(
        {"render_sha256": "c" * 64, "path": "renders/final.mp4"},
        metadata={"episode_id": episode_id, "selected_title": "Tokyo Walk 4K"},
    )
    publish_dir = workspace["episodes_root"] / episode_id / "publish"
    publish_dir.mkdir(parents=True)
    (publish_dir / "package.json").write_bytes(canonical_model_bytes(package))
    UploadLedger(publish_dir, package.channel_target).complete(
        package.idempotency_key, "vid-9x123"
    )

    response = client.get(f"/episodes/{episode_id}/publish-status")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["package"]["selected_title"] == "Tokyo Walk 4K"
    assert body["package"]["channel_target"] == "youtube-main"
    assert body["package"]["idempotency_key"] == package.idempotency_key
    assert body["upload"]["status"] == "completed"
    assert body["upload"]["video_id"] == "vid-9x123"
    assert body["remote_link"] == "https://youtu.be/vid-9x123"


def test_approval_bundles_round_trip_through_json() -> None:
    bundles = bundle_approvals(
        [_pending("opr-00000001", "editorial", "a" * 64), _pending("opr-2", "privacy", "b" * 64)]
    )

    restored = ApprovalBundles.model_validate_json(bundles.model_dump_json())

    assert restored == bundles
