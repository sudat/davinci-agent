from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.gates import GatePolicy
from services.gates.phase0b import PHASE_0B_CRITERIA
from services.toolchain.materialize import MaterializeError, materialize_phase0b
from services.toolchain.models import (
    LockError,
    Phase0BToolchainLock,
    ToolchainLock,
    load_lock,
)
from services.toolchain.normalization import NormalizationSection

PARENT = Path("config/toolchains/phase-0a-v1.json")
PIN = Path("config/toolchains/pins/normalize-recipes.json")
OUT = Path("config/toolchains/phase-0b-v1.json")


def test_materialize_merges_parent_and_pin(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0b-v1.json"
    materialize_phase0b(PARENT, PIN, destination)
    lock = load_lock(destination)
    assert isinstance(lock, Phase0BToolchainLock)
    parent = load_lock(PARENT)
    assert isinstance(parent, ToolchainLock)
    assert lock.ffmpeg.ffmpeg.sha256 == parent.ffmpeg.ffmpeg.sha256
    assert lock.ffmpeg.ffprobe.sha256 == parent.ffmpeg.ffprobe.sha256
    assert lock.python == parent.python
    assert lock.resolve == parent.resolve
    assert lock.smoke.ffmpeg_normalize.status == "pending"


def test_materialize_is_idempotent_for_identical_output(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0b-v1.json"
    materialize_phase0b(PARENT, PIN, destination)
    payload = destination.read_bytes()
    materialize_phase0b(PARENT, PIN, destination)
    assert destination.read_bytes() == payload


def test_materialize_rejects_differing_existing_output(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0b-v1.json"
    materialize_phase0b(PARENT, PIN, destination)
    destination.write_bytes(destination.read_bytes() + b" ")
    with pytest.raises(MaterializeError, match="differs"):
        materialize_phase0b(PARENT, PIN, destination)


def test_materialize_rejects_missing_pin(tmp_path: Path) -> None:
    with pytest.raises((MaterializeError, LockError), match="pin"):
        materialize_phase0b(PARENT, tmp_path / "missing.json", tmp_path / "out.json")


def test_materialize_rejects_noncanonical_pin(tmp_path: Path) -> None:
    section = NormalizationSection.model_validate_json(PIN.read_bytes())
    payload = json.loads(PIN.read_bytes())
    del payload["schema_version"]
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(payload, indent=2))
    assert section.schema_version == "normalize-recipes-v1"
    with pytest.raises(MaterializeError, match="pin"):
        materialize_phase0b(PARENT, pin, tmp_path / "out.json")


def test_phase0b_policy_requires_exact_criteria_and_single_parent() -> None:
    payload = {
        "schema_version": "gate-policy-v1",
        "gate_id": "phase-0b",
        "gate_version": "v1",
        "parent_gate_result_hashes": ["a" * 64],
        "criteria": list(PHASE_0B_CRITERIA),
        "toolchain_lock_sha256": "b" * 64,
        "fixture_manifest_sha256": "c" * 64,
        "golden_sha256": "d" * 64,
        "prerequisite_bindings": [],
        "capability_allowlist": [],
    }
    policy = GatePolicy.model_validate(payload)
    assert policy.gate_id == "phase-0b"
    payload["criteria"] = [*payload["criteria"], "phase-0b-extra-stop"]
    with pytest.raises(ValidationError, match="phase_0b_criteria"):
        GatePolicy.model_validate(payload)
    payload["criteria"] = list(PHASE_0B_CRITERIA)
    payload["parent_gate_result_hashes"] = []
    with pytest.raises(ValidationError, match="phase_0b_parent"):
        GatePolicy.model_validate(payload)
    payload["parent_gate_result_hashes"] = ["a" * 64]
    payload["capability_allowlist"] = ["base_cut"]
    with pytest.raises(ValidationError, match="phase_0b_capabilities"):
        GatePolicy.model_validate(payload)
