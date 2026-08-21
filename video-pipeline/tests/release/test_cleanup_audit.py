"""Blocker-fix addition: post-review cleanup audit (plan F3).

``cleanup_audit`` runs AFTER the review worktree is removed: it proves the
release candidate is still byte-identical (recompute + read-only), records
the removed path as absent, and hash-binds every retained evidence tree so
later lanes can detect cleanup-time evidence tampering.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from services.release.cleanup_audit import (
    CleanupAuditError,
    RemovedPath,
    RetainedTree,
    audit_cleanup,
    tree_digest,
)
from services.release.verify import verify_candidate
from tests.release.support import build_test_candidate


def test_cleanup_audit_receipt_binds_candidate_and_evidence(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "evidence"
    (evidence / "reports").mkdir(parents=True)
    (evidence / "reports" / "raw.md").write_text("PASS\n")
    removed = tmp_path / "review-worktree"

    receipt = audit_cleanup(
        candidate=candidate,
        removed=(removed,),
        retained_evidence=(evidence,),
    )
    verification = verify_candidate(candidate, recompute=True, require_readonly=True)
    assert receipt.candidate_id == verification.candidate_id
    assert receipt.manifest_sha256 == verification.manifest_sha256
    assert receipt.candidate_readonly is True
    assert receipt.identity_unchanged is True
    assert receipt.removed == (RemovedPath(path=str(removed), exists=False),)
    assert len(receipt.retained) == 1
    retained = receipt.retained[0]
    assert retained == RetainedTree(
        path=str(evidence),
        file_count=1,
        tree_sha256=tree_digest(evidence),
    )


def test_cleanup_audit_refuses_when_removed_path_still_exists(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    still_here = tmp_path / "still-here"
    still_here.mkdir()
    with pytest.raises(CleanupAuditError, match="removed-path-present"):
        audit_cleanup(candidate=candidate, removed=(still_here,), retained_evidence=())


def test_cleanup_audit_detects_candidate_tamper(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    writable = tmp_path / "tampered"
    shutil.copytree(candidate, writable)
    for path in writable.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
        else:
            path.chmod(0o755)
    (writable / "identity.json").chmod(0o644)
    (writable / "identity.json").write_bytes(
        (writable / "identity.json").read_bytes() + b" "
    )
    with pytest.raises(CleanupAuditError):
        audit_cleanup(candidate=writable, removed=(), retained_evidence=())
    del sha


def test_tree_digest_changes_when_evidence_changes(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.json").write_text("{}")
    before = tree_digest(tree)
    (tree / "b.json").write_text("{}")
    assert tree_digest(tree) != before
    assert tree_digest(tree) == tree_digest(tree)


def test_cli_writes_receipt_and_exits_zero(tmp_path: Path, capsys) -> None:
    from services.release.cleanup_audit import main as cleanup_main  # noqa: PLC0415

    _repo, candidate, _sha = build_test_candidate(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "r.json").write_text("{}")
    out = tmp_path / "cleanup-receipt.json"
    code = cleanup_main(
        [
            "--candidate", str(candidate),
            "--removed", str(tmp_path / "gone"),
            "--retained-evidence", str(evidence),
            "--out", str(out),
        ]
    )
    assert code == 0
    assert out.is_file()
    assert "cleanup-audit" in capsys.readouterr().out

    code = cleanup_main(
        [
            "--candidate", str(candidate),
            "--removed", str(evidence),
            "--retained-evidence", "",
            "--out", str(tmp_path / "receipt2.json"),
        ]
    )
    assert code == 2
