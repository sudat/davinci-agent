"""Snapshot persistence: atomic save plus hash-verified load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.config.models import EpisodeConfig, RetentionPolicy
from services.config.resolver import resolve
from services.config.store import ConfigStoreError, load_snapshot, save_snapshot
from tests.config.test_resolver import system_config


def test_save_and_load_roundtrip_is_byte_identical(tmp_path: Path) -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-store-01"))
    target = tmp_path / "resolved-config.json"

    save_snapshot(resolved, target)
    loaded = load_snapshot(target)

    assert loaded.canonical_bytes() == resolved.canonical_bytes()
    assert loaded.resolved_config_sha256 == resolved.resolved_config_sha256


def test_load_detects_hash_drift_from_tampered_bytes(tmp_path: Path) -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-store-02"))
    target = tmp_path / "resolved-config.json"
    save_snapshot(resolved, target)

    document = json.loads(target.read_text(encoding="utf-8"))
    document["retention"]["rebuildable_days"] = 9999
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfigStoreError, match="hash"):
        load_snapshot(target)


def test_load_missing_snapshot_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigStoreError):
        load_snapshot(tmp_path / "absent.json")


def test_load_malformed_snapshot_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "garbage.json"
    target.write_text("not-json{", encoding="utf-8")

    with pytest.raises(ConfigStoreError):
        load_snapshot(target)


def test_resave_replaces_snapshot_atomically(tmp_path: Path) -> None:
    first = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-store-03"))
    second = resolve(
        system_config(),
        episode=EpisodeConfig(
            episode_id="ep-store-03",
            retention=RetentionPolicy(authoritative="permanent", rebuildable_days=7),
        ),
    )
    target = tmp_path / "resolved-config.json"

    save_snapshot(first, target)
    save_snapshot(second, target)
    loaded = load_snapshot(target)

    assert loaded.resolved_config_sha256 == second.resolved_config_sha256


def test_saved_file_is_valid_system_config_json(tmp_path: Path) -> None:
    resolved = resolve(system_config(), episode=EpisodeConfig(episode_id="ep-store-04"))
    target = tmp_path / "resolved-config.json"
    save_snapshot(resolved, target)

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema_version"] == "resolved-config-v1"
    assert document["episode_id"] == "ep-store-04"
