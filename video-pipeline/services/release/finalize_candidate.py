"""Controller-owned finalization record (Todo 67).

Embeds the complete canonical Todo67 evidence object, its SHA-256, the
candidate path/ID/manifest/source-tree hashes, observed mode bits, and the
start-work/execution ledger suffix hashes. Serialization is canonical and
deterministic: F1 reserializes the embedded object and verifies its hash.
No commit ever follows a successful finalization.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.qa.run_todo import TodoEvidence
from services.release.build_candidate import chmod_readonly
from services.release.errors import ReleaseGateError
from services.release.models import IDENTITY_NAME, GitSha, ModeBits, ReleaseIdentity
from services.release.staging import require_absolute, sha256_bytes, start_work_ledger_suffix
from services.release.verify import verify_candidate

REVOCATION_SUFFIX = ".revoked"
TODO_EVIDENCE_NUMBER = 67


class FinalizationRecord(StrictModel):
    schema_version: Literal["finalization-v1"] = "finalization-v1"
    git_sha: GitSha
    candidate_path: str
    candidate_id: Sha256
    manifest_sha256: Sha256
    source_tree_sha256: Sha256
    entry_count: int
    total_bytes: int
    mode_bits: ModeBits
    task_evidence: TodoEvidence
    task_evidence_sha256: Sha256
    start_work_ledger_suffix_sha256: Sha256
    execution_ledger_sha256: Sha256


def load_task_evidence(path: Path) -> TodoEvidence:
    evidence = TodoEvidence.model_validate_json(path.read_bytes())
    if evidence.todo != TODO_EVIDENCE_NUMBER:
        todo = evidence.todo
        raise ReleaseGateError(
            "forged_report", f"task evidence is for todo {todo}, not {TODO_EVIDENCE_NUMBER}"
        )
    if not evidence.all_observed:
        raise ReleaseGateError("forged_report", "task evidence has unobserved cases")
    return evidence


def finalize(
    candidate: Path,
    git_sha: str,
    task_evidence_path: Path,
    start_work_ledger: Path,
    execution_ledger: Path,
    out: Path,
    *,
    chmod_readonly_requested: bool,
) -> FinalizationRecord:
    if candidate.with_name(candidate.name + REVOCATION_SUFFIX).exists():
        raise ReleaseGateError("revoked_candidate", f"candidate is revoked: {candidate}")
    require_absolute(start_work_ledger, "start-work ledger")
    require_absolute(execution_ledger, "execution ledger")
    evidence = load_task_evidence(task_evidence_path)
    suffix = start_work_ledger_suffix(start_work_ledger)
    if chmod_readonly_requested:
        chmod_readonly(candidate)
    verification = verify_candidate(
        candidate,
        expected_git_sha=git_sha,
        require_h1_binding=True,
        recompute=True,
        require_readonly=True,
    )
    identity = _identity_of(candidate)
    staged_suffix_hash = identity.start_work_ledger_suffix_sha256
    staged_execution_hash = identity.execution_ledger_sha256
    live_suffix_hash = sha256_bytes(suffix)
    live_execution_hash = sha256_file(execution_ledger)
    if live_suffix_hash != staged_suffix_hash:
        raise ReleaseGateError("stale_hash", "start-work ledger drifted since staging")
    if live_execution_hash != staged_execution_hash:
        raise ReleaseGateError("stale_hash", "execution ledger drifted since staging")
    record = FinalizationRecord(
        git_sha=git_sha,
        candidate_path=str(candidate),
        candidate_id=verification.candidate_id,
        manifest_sha256=verification.manifest_sha256,
        source_tree_sha256=verification.source_tree_sha256,
        entry_count=verification.entry_count,
        total_bytes=verification.total_bytes,
        mode_bits=verification.modes,
        task_evidence=evidence,
        task_evidence_sha256=sha256_bytes(canonical_model_bytes(evidence)),
        start_work_ledger_suffix_sha256=live_suffix_hash,
        execution_ledger_sha256=live_execution_hash,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, canonical_model_bytes(record))
    return record


def _identity_of(candidate: Path) -> ReleaseIdentity:
    return ReleaseIdentity.model_validate_json((candidate / IDENTITY_NAME).read_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--task-evidence", type=Path, required=True)
    parser.add_argument("--start-work-ledger", type=Path, required=True)
    parser.add_argument("--execution-ledger", type=Path, required=True)
    parser.add_argument("--chmod-readonly", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        record = finalize(
            arguments.candidate,
            arguments.git_sha,
            arguments.task_evidence,
            arguments.start_work_ledger,
            arguments.execution_ledger,
            arguments.out,
            chmod_readonly_requested=arguments.chmod_readonly,
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(record.candidate_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
