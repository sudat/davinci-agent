from __future__ import annotations

import subprocess
from pathlib import Path

from services.evidence.ledger import append_attempt, append_completion
from services.evidence.models import CommandResult, CommandSpec
from services.foundation_io import atomic_write


def execute_command(bundle: Path, specification: CommandSpec) -> CommandResult:
    append_attempt(bundle, specification)
    completed = subprocess.run(
        specification.argv,
        cwd=specification.cwd,
        check=False,
        capture_output=True,
    )
    raw = bundle / "raw"
    stdout_path = raw / f"{specification.attempt_id}.stdout"
    stderr_path = raw / f"{specification.attempt_id}.stderr"
    atomic_write(stdout_path, completed.stdout)
    atomic_write(stderr_path, completed.stderr)
    append_completion(
        bundle,
        specification,
        completed.returncode,
        stdout_path,
        stderr_path,
    )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
