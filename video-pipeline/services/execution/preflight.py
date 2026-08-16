from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from services.execution.work_init import WorkInitializationError, restore_by_plan_input
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file


class PreflightError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["execution-contract-v1"] = "execution-contract-v1"
    mode: Literal["direct"] = "direct"
    plan_path: str
    plan_input: str
    plan_input_sha256: str
    prd_sha256: str
    agents_sha256: str
    uv_bin: str
    uv_sha256: str
    uv_version: str
    python_bin: str
    python_sha256: str
    python_version: str
    f4_rule_sha256: str
    baseline_git_sha: str
    cwd: str


def _command_output(argv: list[str], cwd: Path) -> str:
    environment = os.environ | {"GIT_MASTER": "1"}
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_execution_contract(
    workspace_root: Path,
    pipeline_root: Path,
    attempt_dir: Path,
    plan_input: Path,
    out: Path,
) -> None:
    try:
        workspace = workspace_root.resolve(strict=True)
        pipeline = pipeline_root.resolve(strict=True)
        attempt = attempt_dir.resolve(strict=True)
        snapshot = plan_input.resolve(strict=True)
        if Path.cwd().resolve() != pipeline:
            raise PreflightError("preflight must run from pipeline root")
        if attempt not in snapshot.parents:
            raise PreflightError("plan input is outside attempt directory")
        record = restore_by_plan_input(workspace / ".omo/start-work/ledger.jsonl", snapshot)
        if record.attempt_dir != attempt:
            raise PreflightError("attempt directory differs from work initialization")
        if record.plan_input != snapshot:
            raise PreflightError("plan input path differs from work initialization")
        if sha256_file(record.plan_path) != record.initial_plan_sha256:
            raise PreflightError("current plan hash drift")
        rule = attempt / "controller-rules/f4-scope.yml"
        rule_receipt = attempt / "controller-rules/f4-scope.sha256"
        recorded_rule_hash = rule_receipt.read_text().strip()
        actual_rule_hash = sha256_file(rule)
        if recorded_rule_hash != actual_rule_hash:
            raise PreflightError("F4 rule hash drift")
        git_dir = workspace / ".git"
        if not git_dir.is_dir():
            raise PreflightError("workspace is not a Git repository")
        roots = _command_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], workspace
        ).splitlines()
        if len(roots) != 1:
            raise PreflightError("workspace must have one baseline root commit")
        baseline = roots[0]
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", baseline, "HEAD"],
            cwd=workspace,
            env=os.environ | {"GIT_MASTER": "1"},
            check=True,
        )
        contract = ExecutionContract(
            plan_path=str(record.plan_path),
            plan_input=str(snapshot),
            plan_input_sha256=sha256_file(snapshot),
            prd_sha256=sha256_file(workspace / "docs/prd/PRD_v4.1.md"),
            agents_sha256=sha256_file(workspace / "AGENTS.md"),
            uv_bin=str(record.uv_bin),
            uv_sha256=record.uv_sha256,
            uv_version=_command_output([str(record.uv_bin), "--version"], pipeline),
            python_bin=str(record.python_bin),
            python_sha256=record.python_sha256,
            python_version=_command_output([str(record.python_bin), "--version"], pipeline),
            f4_rule_sha256=actual_rule_hash,
            baseline_git_sha=baseline,
            cwd=str(pipeline),
        )
        atomic_write(out, canonical_model_bytes(contract))
    except (OSError, subprocess.CalledProcessError, WorkInitializationError) as error:
        raise PreflightError(str(error)) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--plan-input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        build_execution_contract(
            arguments.workspace_root,
            arguments.pipeline_root,
            arguments.attempt_dir,
            arguments.plan_input,
            arguments.out,
        )
    except PreflightError as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
