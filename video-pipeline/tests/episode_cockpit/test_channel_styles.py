"""工程3: channel style explicit save/restore (U07/U08/U09/U10/U17/U22/U24).

Given/When/Then per acceptance condition. Channel styles are runtime
channel-file state: the ONLY writers are the explicit save/restore
endpoints — adoption, rebuild, and consultation flows never touch the
file, and style ops never touch episode job/lock/state.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.channel_styles import ChannelStyleSaveRequest
from services.episode_cockpit.consultation_store import (
    ConsultationPolicyOutcomeV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_consultation,
    append_effective_judgment_once,
    append_policy_outcome_once,
    append_proposal_set,
    latest_adopted_policy,
    now_stamp,
)
from services.episode_cockpit.errors import CockpitConflictError

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


def _make_source(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _save_body(name: str = "calm vlog", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": name,
        "audience_message": "busy parents",
        "structure": "hook then three beats",
        "duration_estimate": "8-10 min",
        "candidate_scenes": ["opening walk", "kitchen"],
        "subtitle_policy": "short lines",
        "audio_policy": "low bed",
        "tempo_policy": "calm",
        "reference_mapping": "ref-a",
        "unused_reasons": "night scenes too dark",
        "unconfirmed": ["ending"],
        "note": None,
        "source": None,
    }
    body.update(overrides)
    return body


def _save_style(
    client: TestClient, channel: str, body: dict[str, object] | None = None
) -> dict[str, object]:
    response = client.post(
        f"/channels/{channel}/style/save", json=body or _save_body(name=f"{channel} style")
    )
    assert response.status_code in (200, 201)
    payload: dict[str, object] = response.json()
    return payload


def _styles_path(workspace: dict[str, Path]) -> Path:
    return workspace["episodes_root"] / "channel-styles.json"


def _adopt_flow(episode_dir: Path) -> None:
    """Run the judgment/adoption path end to end (no channel-style touch)."""
    consultation = ConsultationRecordV1(
        consultation_id="cons-1", created_at=now_stamp(), message="which direction?"
    )
    append_consultation(episode_dir, consultation)
    details = ConsultationProposalDetails(
        audience_message="busy parents",
        structure="hook then three beats",
        duration_estimate="8-10 min",
        subtitle_policy="short lines",
        audio_policy="low bed",
        tempo_policy="calm",
        reference_mapping="ref-a",
        unused_reasons="night scenes too dark",
    )
    append_proposal_set(
        episode_dir,
        ConsultationProposalSetV1(
            consultation_id="cons-1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="calm cut",
                    summary="slow and clear",
                    details=details,
                ),
            ),
        ),
    )
    judgment, _ = append_effective_judgment_once(
        episode_dir,
        consultation_id="cons-1",
        proposal_id="prop-1",
        decision="adopt",
        scope=ConsultationScope(composition=True, appearance=True, audio=True),
        note=None,
    )
    append_policy_outcome_once(
        episode_dir,
        ConsultationPolicyOutcomeV1(
            outcome_id="out-1",
            consultation_id="cons-1",
            judgment_id=judgment.judgment_id,
            proposal_id="prop-1",
            status="connected",
            created_at=now_stamp(),
        ),
    )
    assert latest_adopted_policy(episode_dir) is not None


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Save + get wire shapes
# ---------------------------------------------------------------------------


def test_save_creates_first_version_and_get_summarizes(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    saved = _save_style(client, "ch-a")

    assert saved == {"channel_id": "ch-a", "version": 1, "idempotent": False}

    response = client.get("/channels/ch-a/style")
    assert response.status_code == 200
    payload = response.json()
    assert payload["channel_id"] == "ch-a"
    assert payload["available"] is True
    assert payload["current"] == 1
    assert len(payload["versions"]) == 1
    summary = payload["versions"][0]
    assert set(summary) == {"version", "name", "saved_at"}
    assert summary["version"] == 1
    assert summary["name"] == "ch-a style"
    entry = payload["entry"]
    assert entry["version"] == 1
    assert entry["entry"]["name"] == "ch-a style"
    assert entry["entry"]["audience_message"] == "busy parents"
    assert _styles_path(workspace).is_file()


def test_get_unknown_channel_returns_empty_record(client: TestClient) -> None:
    response = client.get("/channels/fresh-channel/style")

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": "fresh-channel",
        "available": False,
        "current": None,
        "versions": [],
        "entry": None,
    }


def test_get_channels_lists_saved_channels(client: TestClient) -> None:
    empty = client.get("/channels").json()
    assert empty == {"channels": [], "available": False}

    _save_style(client, "ch-b")
    _save_style(client, "ch-a")

    listed = client.get("/channels").json()
    assert listed == {"channels": ["ch-a", "ch-b"], "available": True}


def test_get_style_version_query_returns_full_entry(client: TestClient) -> None:
    _save_style(client, "ch-a", _save_body(name="first"))
    _save_style(client, "ch-a", _save_body(name="second"))

    first = client.get("/channels/ch-a/style?version=1").json()
    assert first["entry"]["version"] == 1
    assert first["entry"]["entry"]["name"] == "first"
    current = client.get("/channels/ch-a/style").json()
    assert current["entry"]["entry"]["name"] == "second"

    missing = client.get("/channels/ch-a/style?version=99")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "style-version-unknown"


# ---------------------------------------------------------------------------
# U10: idempotent + concurrent-style save
# ---------------------------------------------------------------------------


def test_identical_double_save_is_idempotent(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    body = _save_body()
    first = client.post("/channels/ch-a/style/save", json=body)
    assert first.status_code == 201
    before = _styles_path(workspace).read_bytes()

    second = client.post("/channels/ch-a/style/save", json=body)

    assert second.status_code == 200
    assert second.json() == {"channel_id": "ch-a", "version": 1, "idempotent": True}
    assert _styles_path(workspace).read_bytes() == before


def test_concurrent_style_saves_append_once(workspace: dict[str, Path]) -> None:
    first_ops = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    second_ops = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    body = ChannelStyleSaveRequest.model_validate(_save_body())
    version_a, idempotent_a = first_ops.save_channel_style("ch-a", body)
    version_b, idempotent_b = second_ops.save_channel_style("ch-a", body)

    assert (version_a, idempotent_a) == (1, False)
    assert (version_b, idempotent_b) == (1, True)
    record = second_ops.get_channel_style("ch-a")
    assert len(record.versions) == 1
    assert record.current == 1


# ---------------------------------------------------------------------------
# U08: restart restore + two-channel isolation
# ---------------------------------------------------------------------------


def test_new_store_instance_restores_persisted_state(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    _save_style(client, "ch-a", _save_body(name="persisted"))

    fresh_app = create_cockpit_app(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    with TestClient(fresh_app) as fresh_client:
        payload = fresh_client.get("/channels/ch-a/style").json()

    assert payload["available"] is True
    assert payload["current"] == 1
    assert payload["entry"]["entry"]["name"] == "persisted"
    assert payload["entry"]["entry"]["candidate_scenes"] == ["opening walk", "kitchen"]


def test_two_channels_share_one_file_without_cross_read(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    _save_style(client, "ch-a", _save_body(name="alpha"))
    _save_style(client, "ch-b", _save_body(name="beta-one"))
    _save_style(client, "ch-b", _save_body(name="beta-two"))

    style_a = client.get("/channels/ch-a/style").json()
    style_b = client.get("/channels/ch-b/style").json()

    assert style_a["current"] == 1
    assert style_a["entry"]["entry"]["name"] == "alpha"
    assert style_b["current"] == 2
    assert style_b["entry"]["entry"]["name"] == "beta-two"
    assert [item["name"] for item in style_a["versions"]] == ["alpha"]
    assert [item["name"] for item in style_b["versions"]] == ["beta-one", "beta-two"]
    raw = _styles_path(workspace).read_bytes().decode()
    assert '"ch-a"' in raw
    assert '"ch-b"' in raw


# ---------------------------------------------------------------------------
# U24: restore is a forward version; nothing-to-restore is typed
# ---------------------------------------------------------------------------


def test_restore_commits_forward_version_with_chain_intact(client: TestClient) -> None:
    _save_style(client, "ch-a", _save_body(name="first"))
    _save_style(client, "ch-a", _save_body(name="second"))

    response = client.post("/channels/ch-a/style/restore", json={"target_version": 1})

    assert response.status_code == 201
    assert response.json() == {"channel_id": "ch-a", "version": 3, "restored_from": 1}
    payload = client.get("/channels/ch-a/style").json()
    assert payload["current"] == 3
    assert [item["version"] for item in payload["versions"]] == [1, 2, 3]
    assert [item["name"] for item in payload["versions"]] == ["first", "second", "first"]
    assert payload["entry"]["entry"]["name"] == "first"


def test_restore_without_versions_is_nothing_to_restore(client: TestClient) -> None:
    response = client.post("/channels/fresh-channel/style/restore", json={"target_version": 1})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing-to-restore"


def test_restore_unknown_target_version_is_typed_422(client: TestClient) -> None:
    _save_style(client, "ch-a")

    response = client.post("/channels/ch-a/style/restore", json={"target_version": 99})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "style-version-unknown"


def test_restore_is_typed_conflict_at_ops_level(workspace: dict[str, Path]) -> None:
    ops = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )

    with pytest.raises(CockpitConflictError, match="nothing-to-restore"):
        ops.restore_channel_style("fresh-channel", target_version=1)


def test_restore_current_version_returns_current_without_new_version(
    client: TestClient, workspace: dict[str, Path]
) -> None:
    _save_style(client, "ch-a")
    before = _styles_path(workspace).read_bytes()

    response = client.post("/channels/ch-a/style/restore", json={"target_version": 1})

    assert response.status_code == 201
    assert response.json()["version"] == 1
    assert _styles_path(workspace).read_bytes() == before


# ---------------------------------------------------------------------------
# U09: intake pin + status applied_style
# ---------------------------------------------------------------------------


def test_create_episode_pins_channel_and_version(
    client: TestClient, tmp_path: Path
) -> None:
    _save_style(client, "ch-a", _save_body(name="pinned"))
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-pin")),
            "brief_text": "travel vlog",
            "channel": "ch-a",
            "style_version": 1,
        },
    )

    assert created.status_code == 200
    status = client.get(f"/episodes/{created.json()['episode_id']}").json()
    assert status["applied_style"] == {"channel": "ch-a", "version": 1}


def test_create_episode_pin_without_version_uses_current(
    client: TestClient, tmp_path: Path
) -> None:
    _save_style(client, "ch-a", _save_body(name="v1"))
    _save_style(client, "ch-a", _save_body(name="v2"))
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-current")),
            "brief_text": "travel vlog",
            "channel": "ch-a",
        },
    )

    assert created.status_code == 200
    status = client.get(f"/episodes/{created.json()['episode_id']}").json()
    assert status["applied_style"] == {"channel": "ch-a", "version": 2}


def test_create_episode_without_pin_exposes_null_style(
    client: TestClient, tmp_path: Path
) -> None:
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-plain")),
            "brief_text": "travel vlog",
        },
    )

    assert created.status_code == 200
    status = client.get(f"/episodes/{created.json()['episode_id']}").json()
    assert status["applied_style"] is None


def test_create_episode_unknown_channel_is_typed_422(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-unknown")),
            "brief_text": "travel vlog",
            "channel": "nope",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "channel-unknown"
    assert client.get("/episodes").json() == {"episodes": []}


def test_create_episode_unknown_style_version_is_typed_422(
    client: TestClient, tmp_path: Path
) -> None:
    _save_style(client, "ch-a")

    bad_version = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-badver")),
            "brief_text": "travel vlog",
            "channel": "ch-a",
            "style_version": 99,
        },
    )
    assert bad_version.status_code == 422
    assert bad_version.json()["error"]["code"] == "style-version-unknown"

    no_channel = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-nochannel")),
            "brief_text": "travel vlog",
            "style_version": 1,
        },
    )
    assert no_channel.status_code == 422
    assert no_channel.json()["error"]["code"] == "style-version-unknown"
    assert client.get("/episodes").json() == {"episodes": []}


# ---------------------------------------------------------------------------
# U07: judgment/adoption never writes channel styles
# ---------------------------------------------------------------------------


def test_judgment_and_adoption_never_write_channel_styles(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-adopt")),
            "brief_text": "travel vlog",
        },
    )
    episode_dir = workspace["episodes_root"] / str(created.json()["episode_id"])
    styles_path = _styles_path(workspace)
    assert not styles_path.exists()

    _adopt_flow(episode_dir)

    assert not styles_path.exists()


def test_adoption_leaves_existing_styles_byte_identical(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    _save_style(client, "ch-a")
    before = _styles_path(workspace).read_bytes()
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-adopt-seeded")),
            "brief_text": "travel vlog",
        },
    )
    episode_dir = workspace["episodes_root"] / str(created.json()["episode_id"])

    _adopt_flow(episode_dir)

    assert _styles_path(workspace).read_bytes() == before


# ---------------------------------------------------------------------------
# U17: style ops touch zero episode state
# ---------------------------------------------------------------------------


def test_style_ops_touch_zero_episode_state(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-gated")),
            "brief_text": "travel vlog",
        },
    )
    assert created.status_code == 200
    state_before = workspace["state_store"].read_bytes()
    tree_before = _snapshot_tree(workspace["episodes_root"])

    save_response = client.post("/channels/ch-a/style/save", json=_save_body())
    assert save_response.status_code == 201
    restore_response = client.post(
        "/channels/ch-a/style/restore", json={"target_version": 1}
    )
    assert restore_response.status_code == 201

    assert workspace["state_store"].read_bytes() == state_before
    tree_after = _snapshot_tree(workspace["episodes_root"])
    changed = {
        name
        for name in set(tree_before) | set(tree_after)
        if tree_before.get(name) != tree_after.get(name)
    }
    assert changed == {"channel-styles.json"}


# ---------------------------------------------------------------------------
# U22: only the explicit endpoints write styles
# ---------------------------------------------------------------------------


def test_only_explicit_endpoints_write_channel_styles() -> None:
    root = Path(__file__).resolve().parents[2] / "services" / "episode_cockpit"
    writers = [
        path.name
        for path in sorted(root.glob("*.py"))
        if path.name not in ("api_channel_styles.py", "channel_styles.py")
        and (
            ".save_channel_style(" in path.read_text()
            or ".restore_channel_style(" in path.read_text()
        )
    ]

    assert writers == []


def test_rebuild_and_consultation_flows_leave_styles_byte_identical(
    client: TestClient, tmp_path: Path, workspace: dict[str, Path]
) -> None:
    _save_style(client, "ch-a")
    before = _styles_path(workspace).read_bytes()
    created = client.post(
        "/episodes",
        json={
            "source_folder": str(_make_source(tmp_path, "cam-full-flow")),
            "brief_text": "travel vlog",
        },
    )
    episode_id = str(created.json()["episode_id"])
    rebuild = client.post(f"/episodes/{episode_id}/rebuild", json={})
    assert rebuild.status_code == 202

    _adopt_flow(workspace["episodes_root"] / episode_id)

    assert _styles_path(workspace).read_bytes() == before
