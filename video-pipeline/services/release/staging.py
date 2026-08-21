"""Stage the release candidate tree (Todo 67): Git snapshot plus evidence.

The staging tree is prepared at ``<out>.staging`` from the clean worktree at
``git_sha`` (no untracked smuggle), the plan file, the start-work ledger
suffix through todo-66, the execution ledger, phase Gate evidence, the H1
checkpoint with a freshly derived total binding, and task evidence 1..66.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.release.errors import ReleaseGateError
from services.release.input_gates import check_gate_evidence, scan_inputs
from services.release.manifest import tree_hash
from services.release.models import (
    EXECUTION_LEDGER_INPUT,
    GATE_DIRS,
    GATES_INPUT_DIR,
    H1_BINDING_NAME,
    H1_CHECKPOINT_NAME,
    H1_INPUT_DIR,
    IDENTITY_NAME,
    INPUTS_DIR,
    PLAN_INPUT_NAME,
    SOURCE_DIR,
    START_WORK_LEDGER_INPUT,
    TASK_EVIDENCE_COUNT,
    TASK_EVIDENCE_INPUT_DIR,
    TODO_EXCLUSION_MARKER,
    H1BoundWorld,
    H1TotalBinding,
    ReleaseIdentity,
    task_evidence_name,
)

DEFAULT_ALLOW_UNTRACKED: tuple[str, ...] = ("CLAUDE.md",)
GATE_RESULT_NAME = "gate-result.json"


def require_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleaseGateError("missing_release_input", f"{label} must be absolute: {path}")
    return path


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ReleaseGateError("stale_hash", f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def check_clean_tree(repo: Path, git_sha: str, allow_untracked: tuple[str, ...]) -> None:
    """Refuse any drift: HEAD must equal ``git_sha`` and only allowlisted
    repo-root untracked files may exist (no untracked smuggle)."""

    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    if head != git_sha:
        raise ReleaseGateError("stale_hash", f"HEAD {head} != requested {git_sha}")
    for record in _git(repo, "status", "--porcelain=v1", "-z").split(b"\x00"):
        if not record:
            continue
        status = record[:2].decode()
        path = record[3:].decode() if record[2:3] == b" " else record[2:].decode()
        if status == "??":
            if path in allow_untracked:
                continue
            raise ReleaseGateError("stale_hash", f"untracked smuggle refused: {path}")
        raise ReleaseGateError("stale_hash", f"worktree not clean at {git_sha}: {status} {path}")


def stage_source_from_git(repo: Path, git_sha: str, destination: Path) -> None:
    """Materialize ``video-pipeline`` at ``git_sha`` into ``destination``."""

    destination.mkdir(parents=True, exist_ok=False)
    result = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", f"{git_sha}:video-pipeline"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ReleaseGateError(
            "stale_hash", f"git archive failed: {result.stderr.decode(errors='replace').strip()}"
        )
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def start_work_ledger_suffix(ledger: Path) -> bytes:
    """Ledger bytes through todo-66: any row mentioning todo-67 is dropped."""

    require_absolute(ledger, "start-work ledger")
    kept = [
        line
        for line in ledger.read_bytes().splitlines(keepends=True)
        if TODO_EXCLUSION_MARKER.encode() not in line
    ]
    return b"".join(kept)


def build_h1_binding(attempt_dir: Path, git_sha: str) -> tuple[bytes, bytes]:
    """Derive the total H1 binding from the real operator checkpoint."""

    checkpoint_path = attempt_dir / H1_INPUT_DIR / H1_CHECKPOINT_NAME
    if not checkpoint_path.is_file():
        raise ReleaseGateError("missing_release_input", f"missing H1 checkpoint: {checkpoint_path}")
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint: dict[str, object] = json.loads(checkpoint_bytes)
    if checkpoint.get("fixture_only") is not False:
        raise ReleaseGateError("missing_h1_binding", "H1 checkpoint is fixture-only")
    if checkpoint.get("purpose") != "EDITORIAL_APPROVED":
        raise ReleaseGateError("missing_h1_binding", "H1 checkpoint is not EDITORIAL_APPROVED")
    world = H1BoundWorld(
        edit_source_world_sha256=str(checkpoint["edit_source_world_sha256"]),
        final_plan_sha256=str(checkpoint["final_plan_sha256"]),
        final_ir_sha256=str(checkpoint["final_ir_sha256"]),
        final_preview_sha256=str(checkpoint["final_preview_sha256"]),
    )
    binding = H1TotalBinding(
        purpose="operator_checkpoint",
        decision="EDITORIAL_APPROVED",
        checkpoint_sha256=sha256_file(checkpoint_path),
        fixture_only=False,
        episode_id=str(checkpoint["episode_id"]),
        bound_world=world,
        git_sha=git_sha,
    )
    return canonical_model_bytes(binding), checkpoint_bytes


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _write_file(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


class ReleaseStaging:
    """Typed staging inputs for one candidate build."""

    def __init__(
        self,
        repo: Path,
        git_sha: str,
        plan: Path,
        start_work_ledger: Path,
        execution_ledger: Path,
        attempt_dir: Path,
        allow_untracked: tuple[str, ...] = DEFAULT_ALLOW_UNTRACKED,
    ) -> None:
        self.repo = require_absolute(repo, "repo")
        self.git_sha = git_sha
        self.plan = require_absolute(plan, "plan")
        self.start_work_ledger = require_absolute(start_work_ledger, "start-work ledger")
        self.execution_ledger = require_absolute(execution_ledger, "execution ledger")
        self.attempt_dir = require_absolute(attempt_dir, "attempt dir")
        self.allow_untracked = allow_untracked


def stage_release(staging_inputs: ReleaseStaging, out: Path) -> Path:
    """Prepare ``<out>.staging``; returns the staging directory path."""

    require_absolute(out, "candidate out")
    check_clean_tree(staging_inputs.repo, staging_inputs.git_sha, staging_inputs.allow_untracked)
    staging = out.with_name(out.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    stage_source_from_git(staging_inputs.repo, staging_inputs.git_sha, staging / SOURCE_DIR)
    inputs = staging / INPUTS_DIR
    _copy_file(staging_inputs.plan, inputs / PLAN_INPUT_NAME)
    suffix = start_work_ledger_suffix(staging_inputs.start_work_ledger)
    _write_file(inputs / START_WORK_LEDGER_INPUT, suffix)
    _copy_file(staging_inputs.execution_ledger, inputs / EXECUTION_LEDGER_INPUT)
    for gate_dir in GATE_DIRS:
        gate_source = staging_inputs.attempt_dir / gate_dir / GATE_RESULT_NAME
        if not gate_source.is_file():
            raise ReleaseGateError("failed_gate", f"missing gate result: {gate_source}")
        _copy_file(gate_source, inputs / GATES_INPUT_DIR / gate_dir / GATE_RESULT_NAME)
    binding_bytes, checkpoint_bytes = build_h1_binding(
        staging_inputs.attempt_dir, staging_inputs.git_sha
    )
    _write_file(inputs / H1_INPUT_DIR / H1_CHECKPOINT_NAME, checkpoint_bytes)
    _write_file(inputs / H1_INPUT_DIR / H1_BINDING_NAME, binding_bytes)
    for number in range(1, TASK_EVIDENCE_COUNT + 1):
        name = task_evidence_name(number)
        evidence_source = staging_inputs.attempt_dir / name
        if not evidence_source.is_file():
            raise ReleaseGateError("missing_release_input", f"missing task evidence: {name}")
        _copy_file(evidence_source, inputs / TASK_EVIDENCE_INPUT_DIR / name)
    check_gate_evidence(inputs)
    scan_inputs(inputs)
    identity = ReleaseIdentity(
        git_sha=staging_inputs.git_sha,
        source_tree_sha256=tree_hash(staging / SOURCE_DIR),
        plan_sha256=sha256_file(staging_inputs.plan),
        start_work_ledger_suffix_sha256=sha256_bytes(suffix),
        execution_ledger_sha256=sha256_file(staging_inputs.execution_ledger),
    )
    atomic_write(staging / IDENTITY_NAME, canonical_model_bytes(identity))
    return staging


__all__ = [
    "DEFAULT_ALLOW_UNTRACKED",
    "GATE_RESULT_NAME",
    "ReleaseStaging",
    "build_h1_binding",
    "check_clean_tree",
    "require_absolute",
    "sha256_bytes",
    "stage_release",
    "stage_source_from_git",
    "start_work_ledger_suffix",
]
