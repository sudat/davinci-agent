"""Rejection path: revocation retains bytes, resets lanes, rebuild at new SHA."""

from __future__ import annotations

from pathlib import Path

from services.release.build_candidate import acquire_writer_lock
from services.release.build_candidate import main as build_main
from services.release.finalize_candidate import REVOCATION_SUFFIX, finalize
from services.release.revocation import RevocationRecord, revoke_candidate
from services.release.revocation import main as revoke_main
from tests.release.support import (
    build_test_candidate,
    commit_more,
    expect_gate_error,
    make_attempt_dir,
    make_plan,
    todo67_evidence_bytes,
)


def test_10_revocation_retains_bytes_and_records_resets(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    manifest_before = (candidate / "manifest.json").read_bytes()
    out = tmp_path / "revocation.json"
    argv = [
        "--candidate",
        str(candidate),
        "--reason",
        "failed_gate",
        "--detail",
        "phase-3 regression",
        "--out",
        str(out),
    ]
    assert revoke_main(argv) == 0
    record = RevocationRecord.model_validate_json(out.read_bytes())
    assert record.retains_bytes is True
    assert record.resets == ("todo-67", "F1", "F2", "F3", "F4")
    assert record.rebuild == "new-sha-required"
    assert (candidate / "manifest.json").read_bytes() == manifest_before
    marker = candidate.with_name(candidate.name + REVOCATION_SUFFIX)
    assert marker.is_file()


def test_20_double_revocation_refused(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    revoke_candidate(candidate, "stale_hash", "d", tmp_path / "r.json")
    expect_gate_error(
        "revoked_candidate",
        lambda: revoke_candidate(candidate, "stale_hash", "d", tmp_path / "r2.json"),
    )


def test_30_finalize_on_revoked_candidate_refused(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "task-67.json"
    evidence.write_bytes(todo67_evidence_bytes())
    revoke_candidate(candidate, "operator_reject", "d", tmp_path / "r.json")

    def action() -> None:
        finalize(
            candidate,
            sha,
            evidence,
            tmp_path / "ledger.jsonl",
            tmp_path / "execution-ledger.jsonl",
            tmp_path / "finalization" / f"{sha}.json",
            chmod_readonly_requested=True,
        )

    expect_gate_error("revoked_candidate", action)


def test_40_rebuild_same_path_refused_new_sha_path_succeeds(tmp_path: Path) -> None:
    repo, candidate, _sha = build_test_candidate(tmp_path)
    revoke_candidate(candidate, "global_review_reject", "d", tmp_path / "r.json")
    expect_gate_error("duplicate_writer", lambda: acquire_writer_lock(candidate, restart=True))
    new_sha = commit_more(repo)
    new_args = [
        "--repo",
        str(repo),
        "--git-sha",
        new_sha,
        "--out",
        str(tmp_path / "release-candidates" / new_sha),
        "--plan",
        str(make_plan(tmp_path)),
        "--start-work-ledger",
        str(tmp_path / "ledger.jsonl"),
        "--execution-ledger",
        str(tmp_path / "execution-ledger.jsonl"),
        "--attempt-dir",
        str(make_attempt_dir(tmp_path)),
        "--receipt",
        str(tmp_path / "receipt2.json"),
    ]
    assert build_main([*new_args, "--phase", "all"]) == 0
    assert (tmp_path / "release-candidates" / new_sha).is_dir()
    assert (tmp_path / "receipt2.json").is_file()
