from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

PLAN_SHA: Final = "a" * 64


def _canonical(value: dict[str, str]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def preflight_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    pipeline = workspace / "video-pipeline"
    attempt = workspace / ".omo/start-work/attempts/work"
    plan = workspace / ".omo/plans/plan.md"
    ledger = workspace / ".omo/start-work/ledger.jsonl"
    rules = attempt / "controller-rules/f4-scope.yml"
    pipeline.mkdir(parents=True)
    plan.parent.mkdir(parents=True)
    rules.parent.mkdir(parents=True)
    plan.write_text("plan\n")
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    plan_input = attempt / "plan-input.md"
    plan_input.write_bytes(plan.read_bytes())
    rules.write_text("rules: []\n")
    (rules.parent / "f4-scope.sha256").write_text(
        hashlib.sha256(rules.read_bytes()).hexdigest() + "\n"
    )
    (workspace / "AGENTS.md").write_text("agent\n")
    prd = workspace / "docs/prd/PRD_v4.1.md"
    prd.parent.mkdir(parents=True)
    prd.write_text("prd\n")
    work_id = hashlib.sha256(str(plan).encode() + b"\x00" + bytes.fromhex(plan_sha)).hexdigest()
    tool_path = Path(sys.executable).resolve()
    tool_hash = hashlib.sha256(tool_path.read_bytes()).hexdigest()
    row = {
        "event": "work-initialized",
        "plan_path": str(plan),
        "initial_plan_sha256": plan_sha,
        "execution_work_id": work_id,
        "attempt_dir": str(attempt),
        "plan_input": str(plan_input),
        "execution_ledger": str(attempt / "execution-ledger.jsonl"),
        "uv_bin": str(tool_path),
        "uv_sha256": tool_hash,
        "python_bin": str(tool_path),
        "python_sha256": tool_hash,
        "created_at": "2026-01-01T00:00:00Z",
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(_canonical(row) + b"\n")
    env = os.environ | {"GIT_MASTER": "1"}
    subprocess.run(["git", "init", "-b", "main"], cwd=workspace, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, env=env, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=workspace,
        env=env,
        check=True,
    )
    subprocess.run(["git", "add", "AGENTS.md", "docs"], cwd=workspace, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=workspace, env=env, check=True)
    monkeypatch.chdir(pipeline)
    return workspace, pipeline, attempt, plan_input, ledger
