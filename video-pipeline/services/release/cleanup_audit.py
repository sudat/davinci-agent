"""Post-review cleanup audit (plan F3): candidate identity + evidence receipt.

Runs AFTER the review worktree removal: verifies the release candidate is
still byte-identical (manifest recompute, identity, read-only modes),
records each removed path as ABSENT, and hash-binds every retained
evidence tree (sorted relative-path + file hash lines) so later lanes can
detect cleanup-time evidence tampering.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Literal

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.release.errors import ReleaseGateError
from services.release.verify import verify_candidate


class CleanupAuditError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RemovedPath(StrictModel):
    path: str
    exists: Literal[False] = False


class RetainedTree(StrictModel):
    path: str
    file_count: int
    tree_sha256: Sha256


class CleanupReceipt(StrictModel):
    schema_version: Literal["release-cleanup-receipt-v1"] = "release-cleanup-receipt-v1"
    candidate_path: str
    candidate_id: Sha256
    manifest_sha256: Sha256
    candidate_readonly: bool
    identity_unchanged: bool
    removed: tuple[RemovedPath, ...]
    retained: tuple[RetainedTree, ...]


def tree_digest(root: Path) -> str:
    """sha256 over sorted ``relative\0sha256`` lines of the whole tree."""
    lines: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if entry.is_dir():
                stack.append(entry)
                continue
            relative = entry.relative_to(root).as_posix()
            lines.append(f"{relative}\x00{sha256_file(entry)}\n")
    digest = hashlib.sha256()
    for line in sorted(lines):
        digest.update(line.encode())
    return digest.hexdigest()


def audit_cleanup(
    *,
    candidate: Path,
    removed: tuple[Path, ...],
    retained_evidence: tuple[Path, ...],
) -> CleanupReceipt:
    try:
        verification = verify_candidate(
            candidate, recompute=True, require_readonly=True
        )
    except ReleaseGateError as error:
        raise CleanupAuditError(
            "candidate-drift", f"candidate verification failed: {error}"
        ) from error
    removed_records: list[RemovedPath] = []
    for path in removed:
        if path.exists():
            raise CleanupAuditError(
                "removed-path-present",
                f"cleanup path still exists: {path}",
            )
        removed_records.append(RemovedPath(path=str(path)))
    retained_records = [
        RetainedTree(
            path=str(path),
            file_count=sum(1 for item in path.rglob("*") if item.is_file()),
            tree_sha256=tree_digest(path),
        )
        for path in retained_evidence
        if path.is_dir()
    ]
    return CleanupReceipt(
        candidate_path=str(candidate),
        candidate_id=verification.candidate_id,
        manifest_sha256=verification.manifest_sha256,
        candidate_readonly=verification.modes.all_readonly,
        identity_unchanged=True,
        removed=tuple(removed_records),
        retained=tuple(retained_records),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cleanup_audit")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--removed", type=Path, action="append", default=[])
    parser.add_argument("--retained-evidence", default="")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    retained = tuple(
        Path(item) for item in arguments.retained_evidence.split(",") if item.strip()
    )
    try:
        receipt = audit_cleanup(
            candidate=arguments.candidate,
            removed=tuple(arguments.removed),
            retained_evidence=retained,
        )
        atomic_write(arguments.out, canonical_model_bytes(receipt))
    except (CleanupAuditError, ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(f"cleanup-audit: candidate {receipt.candidate_id[:16]}… unchanged")
    print(f"cleanup-audit: receipt {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CleanupAuditError",
    "CleanupReceipt",
    "RemovedPath",
    "RetainedTree",
    "audit_cleanup",
    "main",
    "tree_digest",
]
