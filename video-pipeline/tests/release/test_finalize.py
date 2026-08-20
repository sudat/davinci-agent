"""Finalization record: canonical embedding, determinism, refusals."""

from __future__ import annotations

import hashlib
from pathlib import Path

from services.foundation_io import canonical_model_bytes
from services.qa.run_todo import TodoEvidence
from services.release.finalize_candidate import (
    FinalizationRecord,
    finalize,
)
from services.release.finalize_candidate import (
    main as finalize_main,
)
from tests.release.support import (
    build_test_candidate,
    expect_gate_error,
    make_writable,
    todo67_evidence_bytes,
)


def finalize_args(candidate: Path, sha: str, tmp: Path, evidence: Path) -> list[str]:
    return [
        "--candidate",
        str(candidate),
        "--git-sha",
        sha,
        "--task-evidence",
        str(evidence),
        "--start-work-ledger",
        str(tmp / "ledger.jsonl"),
        "--execution-ledger",
        str(tmp / "execution-ledger.jsonl"),
        "--chmod-readonly",
        "--out",
        str(tmp / "finalization" / f"{sha}.json"),
    ]


def call_finalize(candidate: Path, sha: str, tmp: Path, evidence: Path) -> None:
    finalize(
        candidate,
        sha,
        evidence,
        tmp / "ledger.jsonl",
        tmp / "execution-ledger.jsonl",
        tmp / "finalization" / f"{sha}.json",
        chmod_readonly_requested=True,
    )


def test_10_happy_record_embeds_evidence_and_hashes(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    out = tmp_path / "finalization" / f"{sha}.json"
    assert finalize_main(finalize_args(candidate, sha, tmp_path, evidence)) == 0
    record = FinalizationRecord.model_validate_json(out.read_bytes())
    assert record.candidate_path == str(candidate)
    assert record.task_evidence.todo == 67
    assert (
        hashlib.sha256(canonical_model_bytes(record.task_evidence)).hexdigest()
        == record.task_evidence_sha256
    )
    assert record.mode_bits.all_readonly is True
    assert record.start_work_ledger_suffix_sha256
    assert record.execution_ledger_sha256


def test_11_f1_style_reserialization_verifies_hash(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    out = tmp_path / "finalization" / f"{sha}.json"
    assert finalize_main(finalize_args(candidate, sha, tmp_path, evidence)) == 0
    raw = out.read_bytes()
    record = FinalizationRecord.model_validate_json(raw)
    assert canonical_model_bytes(record) == raw
    embedded = TodoEvidence.model_validate(record.task_evidence.model_dump())
    assert (
        hashlib.sha256(canonical_model_bytes(embedded)).hexdigest() == record.task_evidence_sha256
    )


def test_12_deterministic_bytes_across_runs(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    out = tmp_path / "finalization" / f"{sha}.json"
    assert finalize_main(finalize_args(candidate, sha, tmp_path, evidence)) == 0
    first = out.read_bytes()
    assert finalize_main(finalize_args(candidate, sha, tmp_path, evidence)) == 0
    assert out.read_bytes() == first


def test_20_wrong_todo_evidence_refused(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes().replace(b'"todo":67', b'"todo":66'))
    expect_gate_error("forged_report", lambda: call_finalize(candidate, sha, tmp_path, evidence))


def test_21_unobserved_evidence_refused(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    payload = todo67_evidence_bytes().replace(b'"all_observed":true', b'"all_observed":false')
    evidence.write_bytes(payload)
    expect_gate_error("forged_report", lambda: call_finalize(candidate, sha, tmp_path, evidence))


def test_30_ledger_drift_refused(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b'{"event":"task-completed","task":"todo-65"}\n')
    expect_gate_error("stale_hash", lambda: call_finalize(candidate, sha, tmp_path, evidence))


def test_40_chmod_flag_repairs_writable_candidate(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    make_writable(candidate / "identity.json")
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    assert finalize_main(finalize_args(candidate, sha, tmp_path, evidence)) == 0
    assert (candidate / "identity.json").stat().st_mode & 0o222 == 0
