"""Task 11 cockpit surface: kit preview manifest reads + operator selection.

The routes are thin over ``KitPreviewOps``; these tests exercise the HTTP
contract (typed 422/404 envelopes, strict revalidation from disk) against
a real TestClient workspace. The mp4 candidates are byte stubs here — the
REAL render path is covered by tests/production_kit/test_preview.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.contracts.primitives import RationalFrameRate
from services.episode_cockpit.app import create_cockpit_app
from services.foundation_io import atomic_write, canonical_model_bytes
from services.production_kit.models import ParameterBoundV1
from services.production_kit.preview import (
    KIT_PREVIEWS_DIR,
    MANIFEST_NAME,
    SELECTIONS_NAME,
    KitPreviewCandidateV1,
    KitPreviewDomainV1,
    KitPreviewManifestV1,
    KitSnippetInfoV1,
    load_selection_record,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    source = tmp_path / "cam-kit"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"fake-mp4")
    response = client.post(
        "/episodes",
        json={"source_folder": str(source), "brief_text": "kit A/B test"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _bounds(pairs: dict[str, tuple[float, float, float]]) -> dict[str, ParameterBoundV1]:
    return {
        param: ParameterBoundV1(min=lo, max=hi, default=default)
        for param, (lo, hi, default) in pairs.items()
    }


def _manifest() -> KitPreviewManifestV1:
    candidates = (
        KitPreviewCandidateV1(
            recipe_id="subtitle/emphasis",
            semantic_intent="keyword_text",
            selection_rationale="intent=keyword_text recipe=subtitle/emphasis; defaults",
            resolved_params={"emphasis_scale": 1.2, "hold_frames": 12},
            parameter_bounds=_bounds(
                {"emphasis_scale": (1.0, 1.5, 1.2), "hold_frames": (6, 30, 12)}
            ),
            file="subtitle/subtitle__emphasis.mp4",
            sha256="a" * 64,
        ),
        KitPreviewCandidateV1(
            recipe_id="subtitle/default",
            semantic_intent="subtitle_track",
            selection_rationale="intent=subtitle_track recipe=subtitle/default; defaults",
            resolved_params={"font_size": 24.0, "line_length": 32.0},
            parameter_bounds=_bounds(
                {"font_size": (12, 48, 24), "line_length": (20, 42, 32)}
            ),
            file="subtitle/subtitle__default.mp4",
            sha256="b" * 64,
        ),
    )
    return KitPreviewManifestV1(
        episode_id="ep-placeholder",
        domains=(
            KitPreviewDomainV1(
                domain="subtitle",
                intents=("keyword_text", "subtitle_track"),
                snippet=KitSnippetInfoV1(
                    origin="explicit",
                    media_path="/fixtures/kit-fixture.mp4",
                    media_sha256="c" * 64,
                    rate=RationalFrameRate(num=15, den=1),
                    start_frame=0,
                    end_frame=45,
                    duration_seconds=3.0,
                ),
                candidates=candidates,
            ),
        ),
    )


def _plant_manifest(episode_root: Path, episode_id: str) -> None:
    manifest = _manifest().model_copy(update={"episode_id": episode_id})
    previews = episode_root / KIT_PREVIEWS_DIR
    (previews / "subtitle").mkdir(parents=True, exist_ok=True)
    for name in ("subtitle__emphasis.mp4", "subtitle__default.mp4"):
        (previews / "subtitle" / name).write_bytes(b"fake-mp4-bytes")
    atomic_write(previews / MANIFEST_NAME, canonical_model_bytes(manifest))


# ---------------------------------------------------------------------------
# (a) GET manifest listing
# ---------------------------------------------------------------------------


def test_get_before_generation_is_an_honest_stub(
    client: TestClient, tmp_path: Path
) -> None:
    episode_id = _create_episode(client, tmp_path)

    response = client.get(f"/episodes/{episode_id}/kit-previews")

    assert response.status_code == 200
    assert response.json() == {"available": False, "domains": []}


def test_get_lists_manifest_domains_and_candidates(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.get(f"/episodes/{episode_id}/kit-previews")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    domain = body["domains"][0]
    assert domain["domain"] == "subtitle"
    assert [c["recipe_id"] for c in domain["candidates"]] == [
        "subtitle/emphasis",
        "subtitle/default",
    ]
    assert domain["selection"] is None


def test_get_with_corrupt_manifest_is_typed_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    previews = workspace["episodes_root"] / episode_id / KIT_PREVIEWS_DIR
    previews.mkdir(parents=True)
    (previews / MANIFEST_NAME).write_bytes(b"{not-json")

    response = client.get(f"/episodes/{episode_id}/kit-previews")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "kit-manifest-invalid"


# ---------------------------------------------------------------------------
# (b) POST operator selection
# ---------------------------------------------------------------------------


def test_select_records_choice_and_round_trips(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select",
        json={"recipe_id": "subtitle/default", "note": "デフォルトで十分"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_intent"] == "subtitle_track"
    record_path = workspace["episodes_root"] / episode_id / SELECTIONS_NAME
    record = load_selection_record(record_path)
    assert record.entries[-1].recipe_id == "subtitle/default"
    # GET now surfaces the recorded selection
    listed = client.get(f"/episodes/{episode_id}/kit-previews").json()
    assert listed["domains"][0]["selection"] == "subtitle/default"

    # a second choice supersedes (append-only, latest wins)
    again = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select",
        json={"recipe_id": "subtitle/emphasis"},
    )
    assert again.status_code == 200
    assert load_selection_record(record_path).entries[-1].recipe_id == "subtitle/emphasis"


def test_select_none_choice_records_current_state_keep(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select",
        json={"recipe_id": None, "note": "どちらも不要"},
    )

    assert response.status_code == 200
    record = load_selection_record(workspace["episodes_root"] / episode_id / SELECTIONS_NAME)
    assert record.entries[-1].recipe_id is None


def test_select_unknown_recipe_is_typed_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select",
        json={"recipe_id": "subtitle/does-not-exist"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "kit-recipe-not-candidate"


def test_select_unknown_domain_is_typed_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/color/select",
        json={"recipe_id": None, "note": "n/a"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "kit-domain-unknown"


def test_select_without_manifest_is_404(
    client: TestClient, tmp_path: Path
) -> None:
    episode_id = _create_episode(client, tmp_path)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select",
        json={"recipe_id": None, "note": "まだ"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "kit-previews-not-found"


def test_select_without_choice_or_note_is_validation_422(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/kit-previews/subtitle/select", json={}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation-error"


# ---------------------------------------------------------------------------
# (a) GET manifest listing/{domain}/{file}
# ---------------------------------------------------------------------------


def test_candidate_file_is_served(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_id = _create_episode(client, tmp_path)
    _plant_manifest(workspace["episodes_root"] / episode_id, episode_id)

    response = client.get(
        f"/episodes/{episode_id}/kit-previews/subtitle/subtitle__default.mp4"
    )

    assert response.status_code == 200
    assert response.content == b"fake-mp4-bytes"


def test_traversal_file_name_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    episode_id = _create_episode(client, tmp_path)

    for name in ("..%2F..%2Fstate.db", "manifest.json", "no-such.mp4"):
        response = client.get(f"/episodes/{episode_id}/kit-previews/subtitle/{name}")
        assert response.status_code == 404, name
