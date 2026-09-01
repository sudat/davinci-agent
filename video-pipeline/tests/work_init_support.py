"""Hermetic work-initialization seeding for tests.

The operator's real ledger (``.omo/start-work/ledger.jsonl``) is runtime
state whose active rows track whichever plan the operator is currently
running — tests must never depend on it or append to it. ``seed_work_init``
builds a temporary ledger whose single row still passes the FULL
``restore_for_plan`` verification: truthful uv/python binary hashes, exact
work-id derivation, byte-identical plan input, and temporary mutable paths.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MissingUvError(Exception):
    executable: str

    def __str__(self) -> str:
        return f"seed_work_init requires {self.executable} on PATH"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_work_init(root: Path) -> tuple[Path, Path]:
    """Seed one active work-initialized row under ``root``.

    Returns ``(ledger_path, plan_path)`` accepted by ``restore_for_plan``.
    """
    uv_on_path = shutil.which("uv")
    if uv_on_path is None:
        raise MissingUvError(executable="uv")
    resolved_root = root.resolve()
    plan = resolved_root / ".omo/plans/seeded-plan.md"
    attempt_dir = resolved_root / ".omo/start-work/attempts/seeded"
    plan_input = attempt_dir / "plan-input.md"
    ledger = resolved_root / ".omo/start-work/ledger.jsonl"
    if attempt_dir.exists():
        shutil.rmtree(attempt_dir)
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan_input.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("seeded plan\n")
    plan_input.write_bytes(plan.read_bytes())
    plan_sha = _sha256(plan)
    work_id = hashlib.sha256(str(plan).encode() + b"\x00" + bytes.fromhex(plan_sha)).hexdigest()
    uv_bin = Path(uv_on_path).resolve()
    python_bin = Path(sys.executable).resolve()
    row = {
        "event": "work-initialized",
        "plan_path": str(plan),
        "initial_plan_sha256": plan_sha,
        "execution_work_id": work_id,
        "attempt_dir": str(attempt_dir),
        "plan_input": str(plan_input),
        "execution_ledger": str(plan_input.parent / "execution-ledger.jsonl"),
        "uv_bin": str(uv_bin),
        "uv_sha256": _sha256(uv_bin),
        "python_bin": str(python_bin),
        "python_sha256": _sha256(python_bin),
        "created_at": "2026-01-01T00:00:00Z",
    }
    ledger.write_bytes(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return ledger, plan
