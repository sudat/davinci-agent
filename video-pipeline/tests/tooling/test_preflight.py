from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.execution.preflight import PreflightError, build_execution_contract


def test_preflight_is_byte_idempotent(
    preflight_workspace: tuple[Path, Path, Path, Path, Path],
) -> None:
    workspace, pipeline, attempt, plan_input, _ledger = preflight_workspace
    out = attempt / "execution-contract.json"

    build_execution_contract(workspace, pipeline, attempt, plan_input, out)
    first = out.read_bytes()
    build_execution_contract(workspace, pipeline, attempt, plan_input, out)

    assert out.read_bytes() == first
    assert json.loads(first)["mode"] == "direct"


@pytest.mark.parametrize("drift", ["plan", "tool", "rule"])
def test_preflight_rejects_bound_input_drift(
    preflight_workspace: tuple[Path, Path, Path, Path, Path],
    drift: str,
) -> None:
    workspace, pipeline, attempt, plan_input, ledger = preflight_workspace
    if drift == "plan":
        plan_input.write_text("changed\n")
    elif drift == "tool":
        row = json.loads(ledger.read_text())
        row["uv_sha256"] = "0" * 64
        ledger.write_text(json.dumps(row) + "\n")
    else:
        (attempt / "controller-rules/f4-scope.yml").unlink()

    with pytest.raises(PreflightError):
        build_execution_contract(
            workspace,
            pipeline,
            attempt,
            plan_input,
            attempt / "execution-contract.json",
        )


def test_preflight_contract_binds_recorded_tool_hash(
    preflight_workspace: tuple[Path, Path, Path, Path, Path],
) -> None:
    workspace, pipeline, attempt, plan_input, ledger = preflight_workspace
    out = attempt / "execution-contract.json"

    build_execution_contract(workspace, pipeline, attempt, plan_input, out)

    row = json.loads(ledger.read_text())
    contract = json.loads(out.read_text())
    assert contract["uv_sha256"] == row["uv_sha256"]
    assert contract["plan_input_sha256"] == hashlib.sha256(plan_input.read_bytes()).hexdigest()
