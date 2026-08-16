"""Hash-chained evidence recording for Resolve bridge CLI runs."""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from services.evidence.ledger import append_attempt, append_completion
from services.evidence.models import CommandSpec, ExitCodeAssertion, StdoutContainsAssertion
from services.foundation_io import atomic_write


@dataclass(frozen=True, slots=True)
class CliRunRecord:
    argv: list[str]
    exit_code: int
    expected_exit: int
    stdout: str
    stderr: str
    observe: str


def invoked_argv(module: str) -> list[str]:
    return [sys.executable, "-m", module, *sys.argv[1:]]


def record_cli_run(bundle: Path, record: CliRunRecord) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    attempt_id = f"todo15-{uuid.uuid4().hex[:12]}"
    stdout_path = bundle / f"{attempt_id}.stdout.txt"
    stderr_path = bundle / f"{attempt_id}.stderr.txt"
    atomic_write(stdout_path, record.stdout.encode())
    atomic_write(stderr_path, record.stderr.encode())
    specification = CommandSpec(
        attempt_id=attempt_id,
        argv=tuple(record.argv),
        cwd=Path.cwd(),
        inputs=(),
        outputs=(stdout_path, stderr_path),
        assertions=(
            ExitCodeAssertion(
                expected=record.expected_exit, passed=record.exit_code == record.expected_exit
            ),
            StdoutContainsAssertion(
                expected=record.observe, passed=record.observe in record.stdout
            ),
        ),
    )
    append_attempt(bundle, specification)
    append_completion(bundle, specification, record.exit_code, stdout_path, stderr_path)
