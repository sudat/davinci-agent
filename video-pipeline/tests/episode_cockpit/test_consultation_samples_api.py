"""Sample request API (wave-1 surface): reserve → render → preview.

Hermetic TestClient suite over a seeded episode (review store with an
AV plan + one adopted consultation): the render seam
(``services.cli.sample_render.render_sample_now``) is monkeypatched
with a fake that publishes through the REAL journal/manifest/budget
helpers, so resend recovery and the preview route exercise the true
read paths.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_core import PydanticCustomError

from services.cli import sample_render
from services.compile.sample_projection import (
    project_sample_ir,
    sample_total_seconds,
)
from services.contracts.primitives import RecordFrameSpan
from services.contracts.timeline_ir import TimelineIr0C
from services.episode_cockpit import episode_files, sample_observation_router
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.consultation_selection_budget import (
    attempt_for,
    reserve_preview,
    settle_preview,
)
from services.episode_cockpit.sample_complete import write_published_manifest
from services.episode_cockpit.sample_identity import (
    SAMPLE_PREVIEW_NAME,
    derive_sample_id,
    sample_dir,
    sample_identity_digest,
)
from services.episode_cockpit.sample_journal import (
    load_sample_events,
    record_sample_success,
)
from services.job_runner.cas import (
    ApprovalRef,
    TransitionPayload,
    apply_transition,
    current_job_state,
)
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import (
    APPROVAL_PURPOSE_EDITORIAL,
    APPROVAL_PURPOSE_FINAL,
    MAIN_PATH,
)
from services.media_intelligence.sample_observation import SampleObservationError
from services.review_command.store import initialize_store
from services.validate.edit_commit_schema import tuplize
from tests.episode_cockpit.test_full_authorization_binding import (
    _seed_consultation,
    _seed_plan_av,
)

VIDEO = b"fake-sample-bytes"


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"], episodes_root=workspace["episodes_root"]
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "sample e2e"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_episode(workspace: dict[str, Path], episode_id: str) -> Path:
    episode_dir = workspace["episodes_root"] / episode_id
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    initialize_store(
        _seed_plan_av(),
        episode_dir / "review" / "events.jsonl",
        store_dir,
    )
    _seed_consultation(episode_dir)
    return episode_dir


def _payload(
    operation_id: str = "op-1",
    windows: tuple[tuple[int, int], ...] = ((0, 30),),
    consultation_id: str = "c1",
) -> dict[str, Any]:
    return {
        "consultation_id": consultation_id,
        "judgment_id": "j1",
        "operation_id": operation_id,
        "windows": [
            {"start_frame": start, "end_frame": end} for start, end in windows
        ],
    }


def _install_fake_render(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], seq_start: int = 1
) -> None:
    seq = itertools.count(seq_start)

    def fake_render(
        episode_root: Path, identity: Any, *, sample_attempt_id: str, **_: Any
    ) -> dict[str, Any]:
        calls.append(sample_attempt_id)
        attempt_no = next(seq)
        ir_path = (
            episode_root / "review" / "store" / f"ir-{identity.base_version}.json"
        )
        full_ir = TimelineIr0C.model_validate(tuplize(json.loads(ir_path.read_bytes())))
        spans = list(identity.windows)
        sample_ir = project_sample_ir(full_ir, spans)
        total = sample_total_seconds(spans, full_ir.rate)
        target = sample_dir(episode_root, derive_sample_id(identity))
        target.mkdir(parents=True, exist_ok=True)
        (target / SAMPLE_PREVIEW_NAME).write_bytes(VIDEO)
        budget = attempt_for(
            attempt_no, identity.consultation_id, identity.judgment_id
        )
        reserve_preview(episode_root, budget, total)
        settle_preview(
            episode_root, budget, preview_seconds=total,
            wall_elapsed=0.05, result="succeeded",
        )
        manifest = write_published_manifest(
            target, identity, sample_ir=sample_ir, total_seconds=total,
            content_sha256=hashlib.sha256(VIDEO).hexdigest(),
            sample_attempt_id=sample_attempt_id,
            budget_entry_id=budget.attempt_id,
            budget_reservation_sequence=budget.reservation_sequence,
            run_id=f"run-fake-{attempt_no:04d}", wall_seconds_used=0.05,
        )
        record_sample_success(
            episode_root, sample_id=manifest.sample_id,
            digest=sample_identity_digest(identity),
            operation_id=identity.operation_id,
            sample_attempt_id=sample_attempt_id,
        )
        return {
            "state": "published",
            "manifest": manifest,
            "sample_attempt_id": sample_attempt_id,
        }

    monkeypatch.setattr(sample_render, "render_sample_now", fake_render)


def test_sample_request_renders_and_preview_serves_bytes(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "published"
    assert body["sample_id"].startswith("sample-")
    assert len(calls) == 1
    preview = client.get(
        f"/episodes/{episode_id}/consultation/samples/{body['sample_id']}/preview"
    )
    assert preview.status_code == 200
    assert preview.content == VIDEO


def test_sample_resend_returns_stored_without_rerender(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    first = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )
    second = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["state"] == "stored"
    assert second.json()["sample_id"] == first.json()["sample_id"]
    assert len(calls) == 1


def test_sample_budget_exhausted_is_422_without_render(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    budget = attempt_for(7, "c1", "j1")
    reserve_preview(episode_dir, budget, 30.0)
    settle_preview(
        episode_dir, budget, preview_seconds=30.0,
        wall_elapsed=0.05, result="succeeded",
    )
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-preview-budget-exhausted"
    assert calls == []


def test_sample_unknown_consultation_is_404(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload(consultation_id="nope"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "consultation-not-found"


def test_sample_invalid_windows_is_422(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload(windows=((30, 30),)),  # zero-length: end <= start
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "sample-windows-invalid"


def test_consultation_view_includes_samples(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)
    posted = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )
    assert posted.status_code == 200

    response = client.get(f"/episodes/{episode_id}/consultation")

    assert response.status_code == 200
    consultations = response.json()["consultations"]
    assert len(consultations) == 1
    samples = consultations[0]["samples"]
    assert len(samples) == 1
    assert samples[0]["sample_id"] == posted.json()["sample_id"]


def _payload_without_windows(
    operation_id: str = "op-auto", consultation_id: str = "c1"
) -> dict[str, Any]:
    return {
        "consultation_id": consultation_id,
        "judgment_id": "j1",
        "operation_id": operation_id,
    }


def test_sample_windows_none_uses_injected_observation_path(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """windows=None flows through the observation seam (injected fixtures, no
    GLM network) and journals the observation record on the reserve line."""
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    observation = json.dumps({"chunks": [{"index": 0}], "insufficient_chunks": []})
    monkeypatch.setattr(
        sample_observation_router,
        "resolve_routed_sample_windows",
        lambda full_ir, root, eid, route_override=None: (
            [RecordFrameSpan(start_frame=0, end_frame=30)],
            observation,
        ),
    )

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload_without_windows(),
    )

    assert response.status_code == 200
    assert response.json()["state"] == "published"
    reserved = [
        e for e in load_sample_events(episode_dir) if e.event == "sample_reserved"
    ]
    assert len(reserved) == 1
    detail = json.loads(reserved[0].detail or "{}")
    assert detail["sample_observation"] == observation


def test_sample_windows_none_blocked_is_typed_422(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable observation path fails the request typed — never position sampling."""
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    def blocked(
        full_ir: Any, root: Path, eid: str, route_override: Any = None,
    ) -> tuple[list[Any], str]:
        raise SampleObservationError(
            "sample-observation-unavailable", "no edit source here"
        )

    monkeypatch.setattr(
        sample_observation_router, "resolve_routed_sample_windows", blocked
    )

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload_without_windows(operation_id="op-blocked"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "sample-observation-unavailable"
    assert calls == []


def test_sample_windows_none_strict_model_failure_is_typed_422_not_500(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict-model failure on the server-pick path (e.g. the window
    widener refusing an empty track) fails typed — never a 500 after the
    observation clips already landed (2026-09-12 acceptance 500)."""
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    def strict_fails(
        full_ir: Any, root: Path, eid: str, route_override: Any = None,
    ) -> tuple[list[Any], str]:
        raise PydanticCustomError("sample-empty-track", "no video interval")

    monkeypatch.setattr(
        sample_observation_router, "resolve_routed_sample_windows", strict_fails
    )

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload_without_windows(operation_id="op-strict"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "sample-observation-unavailable"
    assert calls == []
    assert load_sample_events(episode_dir) == []


def test_sample_windows_none_unexpected_failure_is_typed_422_not_500(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late observation surprise (transport/env failure outside the typed
    seam) fails closed typed — never a 500 after side effects."""
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    def explodes(
        full_ir: Any, root: Path, eid: str, route_override: Any = None,
    ) -> tuple[list[Any], str]:
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(
        sample_observation_router, "resolve_routed_sample_windows", explodes
    )

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json=_payload_without_windows(operation_id="op-boom"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "sample-observation-unavailable"
    assert calls == []
    assert load_sample_events(episode_dir) == []


def test_sample_post_bodies_serialize_as_json(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published + stored POST bodies are plain JSON (the 500's response
    path stays serializable on every state)."""
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    first = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )
    second = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert first.status_code == 200
    assert second.status_code == 200
    for body in (first.json(), second.json()):
        assert set(body) == {"state", "sample_id", "manifest"}
        assert json.loads(json.dumps(body)) == body
        assert body["manifest"]["sample_id"] == body["sample_id"]


def _fast_forward_to_preview_ready(
    workspace: dict[str, Path], episode_id: str
) -> None:
    _fast_forward_to(workspace, episode_id, "PREVIEW_READY")


def _fast_forward_to(workspace: dict[str, Path], episode_id: str, target: str) -> None:
    """Walk the job to the target status (candidate F pins PLAN_COMMITTED)."""

    gated = {
        "EDITORIAL_APPROVED": APPROVAL_PURPOSE_EDITORIAL,
        "FINAL_APPROVED": APPROVAL_PURPOSE_FINAL,
    }
    with StateStore.open(workspace["state_store"]) as store:
        while True:
            current = current_job_state(store, episode_id)
            position = MAIN_PATH.index(current.status)
            if position >= MAIN_PATH.index(target):
                return
            nxt = MAIN_PATH[position + 1]
            payload = (
                TransitionPayload(
                    approval=ApprovalRef(
                        purpose=gated[nxt],
                        target_hash=current.adopted_artifact_hash or "",
                        artifact_ref=f"setup-ref-{gated[nxt]}",
                    )
                )
                if nxt in gated
                else None
            )
            apply_transition(
                store,
                episode_id,
                expected_status=current.status,
                expected_parent_hash=current.adopted_artifact_hash,
                new_status=nxt,
                new_artifact_hash="0" * 64,
                payload=payload,
            )


def test_sample_while_rebuild_active_is_typed_busy_without_render(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judgment (rebuild scheduled) -> sample POST refuses typed-busy.

    The 2026-09-12 incident: the sample request ran its minutes-long
    observation+render while the adoption rebuild was still unresolved,
    the proxy connection died mid-request (bare 500, no journal, no
    traceback), and the base moved underneath anyway. A pending
    judgment-linked rebuild now refuses BEFORE any heavy work with a
    typed 409 — the operator retries after the rebuild lands."""
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    _fast_forward_to_preview_ready(workspace, episode_id)
    monkeypatch.setattr(
        episode_files, "_spawn_runner", lambda *args, **kwargs: None
    )
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    judged = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": "c1",
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": True, "audio": True},
            "note": "busy-gate-repro",
        },
    )
    assert judged.status_code == 202
    judgment_id = judged.json()["judgments"][-1]["judgment_id"]
    before = load_sample_events(episode_dir)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples",
        json={
            "consultation_id": "c1",
            "judgment_id": judgment_id,
            "operation_id": "op-busy-1",
            "windows": [{"start_frame": 0, "end_frame": 30}],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sample-rebuild-running"
    assert calls == []
    assert load_sample_events(episode_dir) == before


def test_sample_request_succeeds_from_plan_committed(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate F: the trial renders from the committed plan + mezzanine.

    The first pass stops at PLAN_COMMITTED (no full preview yet), so the
    sample POST must succeed exactly there — the sample path truncates at
    compile like the adoption rebuild, never waiting for PREVIEW_READY.
    """
    episode_id = _create_episode(client, source_folder)
    _seed_episode(workspace, episode_id)
    _fast_forward_to(workspace, episode_id, "PLAN_COMMITTED")
    calls: list[str] = []
    _install_fake_render(monkeypatch, calls)

    with StateStore.open(workspace["state_store"]) as store:
        assert current_job_state(store, episode_id).status == "PLAN_COMMITTED"
    response = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "published"
    assert body["sample_id"].startswith("sample-")
    assert len(calls) == 1


def test_sample_request_resolves_pre_render_inputs_at_plan_committed(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E cf50732 regression: the sample POST succeeds at PLAN_COMMITTED
    through the REAL resolver. The review bundle is a PREVIEW_READY artifact
    and cannot exist there, so the pre-render set (chain manifest +
    synthesized edit source) feeds the request instead."""
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_episode(workspace, episode_id)
    _fast_forward_to(workspace, episode_id, "PLAN_COMMITTED")
    run_dir = episode_dir / "run"
    (run_dir / "media").mkdir(parents=True, exist_ok=True)
    (run_dir / "episode.json").write_text(
        json.dumps({"episode_id": episode_id}), encoding="utf-8"
    )
    (run_dir / "media" / "edit-source.mov").write_bytes(b"fake-mezzanine")
    assert not (run_dir / "review-bundle.json").exists()

    def fake_default_render(
        _sample_ir: TimelineIr0C,
        _mezzanine: Path,
        out_dir: Path,
        **_kwargs: object,
    ) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / SAMPLE_PREVIEW_NAME).write_bytes(b"plan-committed-sample")

    monkeypatch.setattr(sample_render, "default_sample_render", fake_default_render)

    response = client.post(
        f"/episodes/{episode_id}/consultation/samples", json=_payload()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "published"
    assert body["sample_id"].startswith("sample-")
