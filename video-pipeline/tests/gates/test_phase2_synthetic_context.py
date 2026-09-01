"""Phase-2 synthetic trust boundary (red spec for the fault-harness repair).

Mirrors the approved Phase-1/3 harness seam: ``gate_p2_faults._build_context``
derives a synthetic policy from the supplied frozen policy (rebinding exactly
the transient fields), pairs it with three canonical synthetic parents, a
typed clearly-synthetic H1 checkpoint/display-receipt pair, and a typed
freeze receipt bound to the derived policy. The planned seam does not exist
yet — these tests fail at the missing seam, not on imports.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from services.fixtures.models import Phase2FreezeReceipt
from services.foundation_io import sha256_file
from services.gates import PHASE_1_TECHNICAL_FIXTURES
from services.gates.models import OperatorCheckpointBinding
from services.gates.prerequisite import (
    DISPLAY_RECEIPT_NAME,
    checkpoint_binding,
    verify_editorial_approved_checkpoint,
)
from services.gates.serialization import canonical_gate_bytes
from services.job_runner import gate_p2_checks, gate_p2_faults
from services.job_runner.gate_p2_fake_tree import FaultKnobs
from services.job_runner.gate_phase2 import load_policy

POLICY = Path("config/gates/phase-2-v4.json")
TRANSIENT_FIELDS = (
    "gate_version",
    "parent_gate_result_hashes",
    "toolchain_lock_sha256",
    "fixture_manifest_sha256",
    "prerequisite_bindings",
)


def test_synthetic_context_bytes_are_deterministic() -> None:
    first = gate_p2_faults._build_context(POLICY)
    second = gate_p2_faults._build_context(POLICY)

    assert first.policy_bytes == second.policy_bytes
    assert tuple(p.raw for p in first.parents) == tuple(p.raw for p in second.parents)
    assert first.checkpoint_sha256 == second.checkpoint_sha256


def test_synthetic_policy_changes_exactly_the_transient_bindings() -> None:
    source, _source_sha = load_policy(POLICY)
    context = gate_p2_faults._build_context(POLICY)
    source_fields = source.model_dump(mode="json")
    synthetic_fields = context.policy.model_dump(mode="json")
    for field in TRANSIENT_FIELDS:
        del source_fields[field]
        del synthetic_fields[field]

    assert synthetic_fields == source_fields
    assert context.policy.gate_version == gate_p2_faults.SYNTHETIC_VERSION
    assert context.policy.parent_gate_result_hashes == tuple(
        parent.sha256 for parent in context.parents
    )
    assert context.policy.toolchain_lock_sha256 == sha256_file(
        gate_p2_checks.LOCK_PATH
    )
    combined = hashlib.sha256()
    for fixture_id in gate_p2_checks.PHASE_2_FIXTURES:
        combined.update(
            (gate_p2_checks.MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        )
    assert context.policy.fixture_manifest_sha256 == combined.hexdigest()
    assert len(context.policy.prerequisite_bindings) == 1
    assert isinstance(
        context.policy.prerequisite_bindings[0], OperatorCheckpointBinding
    )
    assert (
        context.policy.prerequisite_bindings[0].checkpoint_sha256
        == context.checkpoint_sha256
    )


def test_three_canonical_passing_typed_parents() -> None:
    context = gate_p2_faults._build_context(POLICY)

    assert tuple((p.result.gate_id, p.relative) for p in context.parents) == (
        ("phase-0a", "phase-0a"),
        ("phase-0b", "phase-0b"),
        ("phase-1-technical", "phase-1-technical"),
    )
    for parent in context.parents:
        assert parent.raw == canonical_gate_bytes(parent.result)
        assert hashlib.sha256(parent.raw).hexdigest() == parent.sha256
        assert parent.result.passed is True
        assert "synthetic" in parent.result.gate_version
    assert len({p.sha256 for p in context.parents}) == 3


def test_synthetic_checkpoint_pair_verifies_with_the_real_verifier(
    tmp_path: Path,
) -> None:
    context = gate_p2_faults._build_context(POLICY)
    checkpoint_path = gate_p2_faults._write_checkpoint(tmp_path, context)

    checkpoint = verify_editorial_approved_checkpoint(checkpoint_path)
    assert "synthetic" in checkpoint.episode_id
    assert checkpoint.episode_id not in PHASE_1_TECHNICAL_FIXTURES
    receipt_bytes = (checkpoint_path.parent / DISPLAY_RECEIPT_NAME).read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest() == (
        checkpoint.display_receipt_sha256
    )
    binding = checkpoint_binding(checkpoint_path)
    assert binding.checkpoint_sha256 == context.checkpoint_sha256
    assert context.policy.prerequisite_bindings[0] == binding


def test_freeze_receipt_is_typed_and_bound_to_derived_policy(
    tmp_path: Path,
) -> None:
    context = gate_p2_faults._build_context(POLICY)
    gate_p2_faults._write_checkpoint(tmp_path, context)
    receipt = context.freeze_receipt

    assert isinstance(receipt, Phase2FreezeReceipt)
    assert receipt.gate_id == "phase-2"
    assert receipt.policy_sha256 == context.policy_sha256
    assert receipt.prerequisite_checkpoint.checkpoint_sha256 == (
        context.checkpoint_sha256
    )
    assert sorted(link.gate_id for link in receipt.parent_gate_results) == sorted(
        gate_p2_checks.PARENT_GATES
    )
    assert {link.sha256 for link in receipt.parent_gate_results} == {
        parent.sha256 for parent in context.parents
    }


def test_context_keeps_the_supplied_policy_frozen_bindings() -> None:
    source, _source_sha = load_policy(POLICY)
    context = gate_p2_faults._build_context(POLICY)

    assert context.policy.golden_sha256 == source.golden_sha256


def test_synthetic_identity_not_frozen_and_files_ephemeral(tmp_path: Path) -> None:
    source_bytes = POLICY.read_bytes()
    _source, source_sha = load_policy(POLICY)
    context = gate_p2_faults._build_context(POLICY)

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        root = Path(temporary)
        outcome = gate_p2_faults._evaluate_fake(root, FaultKnobs(), context)
        assert outcome.result.passed, outcome.mismatches
        assert outcome.result.gate_version == gate_p2_faults.SYNTHETIC_VERSION
        assert outcome.result.policy_sha256 == context.policy_sha256
        assert context.policy_sha256 != source_sha
        assert (root / "phase-0a" / "gate-result.json").is_file()
        assert (root / context.checkpoint_relative).exists()

    assert not root.exists()
    assert POLICY.read_bytes() == source_bytes
