"""Finishing domains status: read-only GET /episodes/{id}/finishing-status.

The route surfaces the writer's three display fields plus the typed
``not-yet-run`` stub. Exercise the HTTP contract (typed 404/422 + strict
revalidation) against a real TestClient workspace — fixtures are byte-level
plants of the minimal projection (the writer's full model lives in
``services.cli._v44_finishing_report.FinishingRunReportV1``; the cockpit reads
a strict projection of it so tests plant hand-written JSON mirroring the
shape verified against the reference episode
``private/reference-episodes/v44-real-01/.../finishing-run.json``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.finishing_status import (
    FINISHING_RUN_RELATIVE,
    QUALITY_DOMAIN_NAMES,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class FinishingPayload(TypedDict):
    schema_version: str
    episode_id: str
    run_id: str
    domain_statuses: dict[str, str]
    domain_justifications: dict[str, str]
    blocked_domains: list[str]
    executor: str
    wall_clock_seconds: float


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


def _create_episode(client: TestClient, tmp_path: Path) -> str:
    source = tmp_path / "cam-finishing"
    source.mkdir(parents=True, exist_ok=True)
    (source / "clip.mp4").write_bytes(b"fake-mp4")
    response = client.post(
        "/episodes",
        json={"source_folder": str(source), "brief_text": "finishing status test"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["episode_id"])


def _plant_finishing_run(episode_root: Path, payload: FinishingPayload) -> None:
    target = episode_root.joinpath(*FINISHING_RUN_RELATIVE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _reference_payload(episode_id: str) -> FinishingPayload:
    """Minimal valid finishing-run shape (projection fields + extras are ignored).

    Mirrors the real writer's three fields plus two ignored extras so the
    cockpit's ``extra=ignore`` contract is exercised.
    """

    return {
        "schema_version": "v44-finishing-run-v1",
        "episode_id": episode_id,
        "run_id": "run-test-001",
        "domain_statuses": {
            "editorial_construction": "applied",
            "subtitle": "applied",
            "audio_finishing": "applied",
            "color_finishing": "applied",
            "framing_motion": "intentionally_not_needed",
            "graphics_presentation": "intentionally_not_needed",
            "delivery_qc": "blocked",
        },
        "domain_justifications": {
            "framing_motion": "no framing/motion treatment requested for this episode",
            "graphics_presentation": (
                "no graphics/presentation treatment requested for this episode"
            ),
            "delivery_qc": "delivery QC did not pass for this episode",
        },
        "blocked_domains": ["delivery_qc"],
        # ignored extras — writer carries these but cockpit does not read them
        "executor": "fake",
        "wall_clock_seconds": 12.5,
    }


# ---------------------------------------------------------------------------
# absent file → honest not-yet-run stub (never an error dump)
# ---------------------------------------------------------------------------


def test_absent_file_is_not_yet_run_stub(
    client: TestClient, tmp_path: Path
) -> None:
    episode_id = _create_episode(client, tmp_path)

    response = client.get(f"/episodes/{episode_id}/finishing-status")

    assert response.status_code == 200
    assert response.json() == {"available": False, "domains": []}


def test_unknown_episode_is_typed_404(client: TestClient) -> None:
    response = client.get("/episodes/ep-does-not-exist/finishing-status")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "episode-not-found"


# ---------------------------------------------------------------------------
# valid file → seven domains in canonical order with justification/blocked
# ---------------------------------------------------------------------------


def test_valid_file_returns_seven_domains_in_canonical_order(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_finishing_run(
        workspace["episodes_root"] / episode_id, _reference_payload(episode_id)
    )

    response = client.get(f"/episodes/{episode_id}/finishing-status")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["episode_id"] == episode_id
    assert body["run_id"] == "run-test-001"
    domains = body["domains"]
    assert [d["domain"] for d in domains] == list(QUALITY_DOMAIN_NAMES)
    # applied domains carry null justification
    applied = {d["domain"]: d for d in domains if d["status"] == "applied"}
    for domain in ("editorial_construction", "subtitle", "audio_finishing", "color_finishing"):
        assert applied[domain]["justification"] is None
        assert applied[domain]["blocked"] is False
    # intentionally_not_needed carries its justification
    framing = next(d for d in domains if d["domain"] == "framing_motion")
    assert framing["status"] == "intentionally_not_needed"
    assert framing["justification"] == "no framing/motion treatment requested for this episode"
    assert framing["blocked"] is False
    # blocked domain is flagged
    qc = next(d for d in domains if d["domain"] == "delivery_qc")
    assert qc["status"] == "blocked"
    assert qc["blocked"] is True
    assert qc["justification"] == "delivery QC did not pass for this episode"


def test_all_four_statuses_are_surfaced(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    payload = _reference_payload(episode_id)
    # add manual_fallback_required to exercise the fourth status
    statuses = payload["domain_statuses"].copy()
    justifications = payload["domain_justifications"].copy()
    statuses["color_finishing"] = "manual_fallback_required"
    justifications["color_finishing"] = "needs manual color pass"
    payload["domain_statuses"] = statuses
    payload["domain_justifications"] = justifications
    _plant_finishing_run(workspace["episodes_root"] / episode_id, payload)

    response = client.get(f"/episodes/{episode_id}/finishing-status")

    assert response.status_code == 200
    by_domain = {d["domain"]: d for d in response.json()["domains"]}
    assert by_domain["color_finishing"]["status"] == "manual_fallback_required"
    assert by_domain["color_finishing"]["justification"] == "needs manual color pass"


# ---------------------------------------------------------------------------
# malformed file → typed 422 finishing-run-invalid
# ---------------------------------------------------------------------------


def test_corrupt_json_is_typed_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    target = workspace["episodes_root"] / episode_id / Path(*FINISHING_RUN_RELATIVE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{not-json")

    response = client.get(f"/episodes/{episode_id}/finishing-status")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "finishing-run-invalid"


def test_wrong_domain_set_is_typed_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    payload = _reference_payload(episode_id)
    statuses = payload["domain_statuses"].copy()
    del statuses["delivery_qc"]
    payload["domain_statuses"] = statuses
    _plant_finishing_run(workspace["episodes_root"] / episode_id, payload)

    response = client.get(f"/episodes/{episode_id}/finishing-status")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "finishing-run-invalid"
