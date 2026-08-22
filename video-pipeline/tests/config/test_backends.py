"""Backend feature flags: typed load, validation, and set_backend persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.config.backends import BackendsConfig, BackendsConfigError, load_backends, set_backend


def _write_backends(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_valid_file_loads_with_typed_enum_values(tmp_path: Path) -> None:
    target = tmp_path / "backends.json"
    _write_backends(
        target,
        {
            "schema_version": "backends-v1",
            "execution_backend": "legacy_direct",
            "analysis_backend": "legacy_local",
            "editorial_contract": "phase1_v1",
        },
    )

    loaded = load_backends(target)

    assert isinstance(loaded, BackendsConfig)
    assert loaded.schema_version == "backends-v1"
    assert loaded.execution_backend == "legacy_direct"
    assert loaded.analysis_backend == "legacy_local"
    assert loaded.editorial_contract == "phase1_v1"


def test_invalid_execution_backend_raises_typed_validation_error(tmp_path: Path) -> None:
    target = tmp_path / "backends.json"
    _write_backends(
        target,
        {
            "schema_version": "backends-v1",
            "execution_backend": "mcpp",
            "analysis_backend": "legacy_local",
            "editorial_contract": "phase1_v1",
        },
    )

    with pytest.raises((BackendsConfigError, ValidationError)):
        load_backends(target)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "backends.json"
    _write_backends(
        target,
        {
            "schema_version": "backends-v1",
            "execution_backend": "legacy_direct",
            "analysis_backend": "legacy_local",
            "editorial_contract": "phase1_v1",
            "oops": 1,
        },
    )

    with pytest.raises((BackendsConfigError, ValidationError)):
        load_backends(target)


def test_set_backend_updates_in_memory_and_on_disk(tmp_path: Path) -> None:
    target = tmp_path / "backends.json"
    _write_backends(
        target,
        {
            "schema_version": "backends-v1",
            "execution_backend": "legacy_direct",
            "analysis_backend": "legacy_local",
            "editorial_contract": "phase1_v1",
        },
    )

    updated = set_backend("execution_backend", "mcp", path=target)

    assert updated.execution_backend == "mcp"

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["execution_backend"] == "mcp"

    reloaded = load_backends(target)
    assert reloaded.execution_backend == "mcp"
    assert reloaded.analysis_backend == "legacy_local"
    assert reloaded.editorial_contract == "phase1_v1"


def test_set_backend_rejects_invalid_enum_value(tmp_path: Path) -> None:
    target = tmp_path / "backends.json"
    _write_backends(
        target,
        {
            "schema_version": "backends-v1",
            "execution_backend": "legacy_direct",
            "analysis_backend": "legacy_local",
            "editorial_contract": "phase1_v1",
        },
    )

    with pytest.raises((BackendsConfigError, ValidationError, ValueError)):
        set_backend("execution_backend", "mcpp", path=target)

    # Ensure the on-disk file was not corrupted by the failed update.
    reloaded = load_backends(target)
    assert reloaded.execution_backend == "legacy_direct"
