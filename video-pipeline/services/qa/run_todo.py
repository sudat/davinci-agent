from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from services.execution.work_init import WorkInitializationError, restore_for_plan
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file


class MatrixCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: str = Field(min_length=1)
    kind: Literal["happy", "failure"]
    argv: tuple[str, ...] = Field(min_length=1)
    stdin_fixture: str | None
    environment: dict[str, str]
    fault_fixture: str | None
    expected_exit: int
    expected_artifacts: tuple[str, ...]
    expected_observation: str


class TodoMatrix(RootModel[tuple[MatrixCase, ...]]):
    model_config = ConfigDict(frozen=True, strict=True)


class ArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: str


class CaseEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: str
    kind: Literal["happy", "failure"]
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    artifacts: tuple[ArtifactEvidence, ...]
    observed: bool


class TodoEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    todo: int
    cases: tuple[CaseEvidence, ...]
    all_observed: bool


def _artifact_evidence(case: MatrixCase, cwd: Path) -> tuple[tuple[ArtifactEvidence, ...], bool]:
    artifacts: list[ArtifactEvidence] = []
    complete = True
    for declared in case.expected_artifacts:
        path = Path(declared)
        resolved = path if path.is_absolute() else cwd / path
        if not resolved.is_file():
            complete = False
            continue
        artifacts.append(
            ArtifactEvidence(path=str(resolved.resolve()), sha256=sha256_file(resolved))
        )
    return tuple(artifacts), complete


def run_matrix(todo: int, matrix: Path, evidence: Path, uv_bin: Path, cwd: Path) -> bool:
    try:
        cases = TodoMatrix.model_validate_json(matrix.read_bytes()).root
    except ValidationError as error:
        raise ValueError(f"invalid QA matrix: {error}") from error
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("QA matrix must contain unique cases")
    observed_cases: list[CaseEvidence] = []
    for case in cases:
        argv = list(case.argv)
        if argv[0] == "uv":
            argv[0] = str(uv_bin.resolve(strict=True))
        stdin = None
        if case.stdin_fixture is not None:
            stdin = Path(case.stdin_fixture).read_text()
        environment = os.environ | case.environment
        if case.fault_fixture is not None:
            environment["QA_FAULT_FIXTURE"] = case.fault_fixture
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            input=stdin,
            check=False,
            capture_output=True,
            text=True,
        )
        artifacts, artifacts_complete = _artifact_evidence(case, cwd)
        combined_output = result.stdout + result.stderr
        observed = (
            result.returncode == case.expected_exit
            and artifacts_complete
            and case.expected_observation in combined_output
        )
        observed_cases.append(
            CaseEvidence(
                case_id=case.case_id,
                kind=case.kind,
                argv=tuple(
                    str(Path(item).resolve()) if index == 0 else item
                    for index, item in enumerate(argv)
                ),
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                artifacts=artifacts,
                observed=observed,
            )
        )
    all_observed = all(case.observed for case in observed_cases)
    atomic_write(
        evidence,
        canonical_model_bytes(
            TodoEvidence(todo=todo, cases=tuple(observed_cases), all_observed=all_observed)
        ),
    )
    return all_observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--todo", type=int, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    workspace = Path.cwd().resolve().parent
    plan = workspace / ".omo/plans/foundation-video-pipeline.md"
    try:
        plan_hash = sha256_file(plan)
        record = restore_for_plan(workspace / ".omo/start-work/ledger.jsonl", plan, plan_hash)
        observed = run_matrix(
            arguments.todo,
            arguments.matrix,
            arguments.evidence,
            record.uv_bin,
            Path.cwd().resolve(),
        )
    except (OSError, ValidationError, ValueError, WorkInitializationError) as error:
        print(error)
        return 2
    return 0 if observed else 1


if __name__ == "__main__":
    raise SystemExit(main())
