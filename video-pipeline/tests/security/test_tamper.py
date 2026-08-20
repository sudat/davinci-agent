"""Attack class 8: stale/tampered evidence is always detected.

Active tampering against the real hash-chained stores: the evidence
bundle ledger (row rewrite, head forgery), the sealed asset registry
(entry swap), and the outbound audit chain (endpoint rewrite). Every
tamper attempt must be caught by verification, and verification itself
must not mutate anything (read-only detection).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.audit.outbound import OutboundAuditError, OutboundRequestAudit, OutboundRequestInput
from services.evidence.ledger import append_attempt, append_completion
from services.evidence.models import CommandSpec, ExitCodeAssertion
from services.evidence.verify import EvidenceVerificationError, verify_bundle
from services.presentation.asset_registry import AssetEntry, RegistrySnapshot


def _spec(cwd: Path, out: Path) -> CommandSpec:
    (cwd / "in.txt").write_text("input", encoding="utf-8")
    (cwd / "out.txt").write_text("output", encoding="utf-8")
    return CommandSpec(
        attempt_id="todo66-attempt",
        argv=("python", "-m", "services.security.probe_local_listeners"),
        cwd=cwd,
        inputs=(cwd / "in.txt",),
        outputs=(cwd / "out.txt",),
        assertions=(ExitCodeAssertion(expected=0, passed=True),),
    )


def _bundle(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    spec = _spec(work, work / "out.txt")
    bundle = tmp_path / "bundle"
    append_attempt(bundle, spec)
    append_completion(bundle, spec, 0, work / "out.txt", work / "out.txt")
    report = verify_bundle(bundle, recompute=True)
    assert report.chain_valid is True
    return bundle


def test_10_honest_bundle_verifies(tmp_path: Path) -> None:
    report = verify_bundle(_bundle(tmp_path), recompute=True)
    assert report.chain_valid is True
    assert report.recomputed_passed is True


def test_11_rewritten_ledger_row_detected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    ledger = bundle / "ledger.jsonl"
    tampered = ledger.read_text(encoding="utf-8").replace("todo66-attempt", "attacker-attempt")
    ledger.write_text(tampered, encoding="utf-8")
    with pytest.raises(EvidenceVerificationError):
        verify_bundle(bundle, recompute=True)
    assert ledger.read_text(encoding="utf-8") == tampered


def test_12_forged_head_detected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    head = bundle / "head.json"
    head.write_text(head.read_text(encoding="utf-8").replace("1", "2"), encoding="utf-8")
    with pytest.raises(EvidenceVerificationError):
        verify_bundle(bundle, recompute=True)


def test_13_truncated_ledger_detected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    ledger = bundle / "ledger.jsonl"
    lines = ledger.read_bytes().splitlines(keepends=True)
    ledger.write_bytes(lines[0])
    with pytest.raises(EvidenceVerificationError):
        verify_bundle(bundle, recompute=True)


def _sealed_registry() -> RegistrySnapshot:
    entry = AssetEntry.model_validate(
        {
            "asset_id": "asset:se-01",
            "sha256": "1" * 64,
            "path": "assets/se-01.wav",
            "usage": "se",
            "territories": ("JP",),
            "effective_date": "2020-01-01",
            "expiry_date": None,
            "license_evidence_ref": "lic/se-01.pdf",
            "attribution": None,
            "content_id_notes": None,
            "approved": True,
        }
    )
    draft = RegistrySnapshot(entries=(entry,), registry_snapshot_sha256="0" * 64)
    return draft.model_copy(update={"registry_snapshot_sha256": draft.content_hash()})


def test_20_registry_entry_swap_breaks_the_seal() -> None:
    sealed = _sealed_registry()
    assert sealed.verify_hash() is True
    entry = sealed.entries[0].model_copy(update={"approved": False})
    rebuilt = RegistrySnapshot(entries=(entry,), registry_snapshot_sha256="0" * 64)
    swapped = rebuilt.model_copy(
        update={"registry_snapshot_sha256": sealed.registry_snapshot_sha256}
    )
    assert swapped.verify_hash() is False
    assert sealed.verify_hash() is True


def test_30_outbound_audit_rewrite_detected(tmp_path: Path) -> None:
    path = tmp_path / "outbound-audit.jsonl"
    audit = OutboundRequestAudit(path)

    def input_at(seq: int) -> OutboundRequestInput:
        return OutboundRequestInput(
            destination_class="cloud",
            endpoint_declared="https://synthetic-fixture.example.test/v1",
            data_classes=("review_instruction_text",),
            policy_decision="allow",
            policy_reason="synthetic fixture",
            request_sha256="e" * 64,
            timestamp_seq=seq,
        )

    audit.record_request(input_at(1))
    audit.record_request(input_at(2))
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "synthetic-fixture.example.test", "evil.example.invalid"
        ),
        encoding="utf-8",
    )
    with pytest.raises(OutboundAuditError, match="chain"):
        OutboundRequestAudit(path).audit_query()
