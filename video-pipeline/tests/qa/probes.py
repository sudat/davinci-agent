from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from services.execution.work_init import (
    WorkInitializationError,
    restore_for_plan,
)
from services.source_snapshot import SnapshotError, create_snapshot


def _canonical(value: dict[str, str]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fixture(root: Path) -> tuple[Path, Path, str, dict[str, str]]:
    plan = root / ".omo/plans/plan.md"
    attempt = root / ".omo/start-work/attempts/work"
    ledger = root / ".omo/start-work/ledger.jsonl"
    plan.parent.mkdir(parents=True)
    attempt.mkdir(parents=True)
    plan.write_text("plan\n")
    plan_input = attempt / "plan-input.md"
    plan_input.write_bytes(plan.read_bytes())
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    work_id = hashlib.sha256(str(plan).encode() + b"\x00" + bytes.fromhex(plan_hash)).hexdigest()
    tool = Path(sys.executable).resolve()
    tool_hash = hashlib.sha256(tool.read_bytes()).hexdigest()
    row = {
        "event": "work-initialized",
        "plan_path": str(plan),
        "initial_plan_sha256": plan_hash,
        "execution_work_id": work_id,
        "attempt_dir": str(attempt),
        "plan_input": str(plan_input),
        "execution_ledger": str(attempt / "execution-ledger.jsonl"),
        "uv_bin": str(tool),
        "uv_sha256": tool_hash,
        "python_bin": str(tool),
        "python_sha256": tool_hash,
        "created_at": "2026-01-01T00:00:00Z",
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(_canonical(row) + b"\n")
    return plan, ledger, plan_hash, row


def _expect_restore_failure(ledger: Path, plan: Path, plan_hash: str) -> None:
    try:
        restore_for_plan(ledger, plan, plan_hash)
    except WorkInitializationError:
        return
    raise AssertionError("restore unexpectedly succeeded")


def _happy_workinit_reuse(root: Path) -> None:
    plan, ledger, plan_hash, _row = _fixture(root)
    first = restore_for_plan(ledger, plan, plan_hash)
    with ledger.open("ab") as stream:
        stream.write(b'{"event":"work-initialized"')
    second = restore_for_plan(ledger, plan, plan_hash)
    assert first.execution_work_id == second.execution_work_id
    assert first.attempt_dir == second.attempt_dir
    assert ledger.read_bytes().count(b'"event":"work-initialized"') == 2


def _happy_boulder_delete_resume(root: Path) -> None:
    plan, ledger, plan_hash, row = _fixture(root)
    boulder = root / ".omo/boulder.json"
    boulder.write_text("stale")
    boulder.unlink()
    output = root / "restore.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.execution.work_init",
            "--ledger",
            str(ledger),
            "--plan-path",
            str(plan),
            "--initial-plan-sha256",
            plan_hash,
            "--out",
            str(output),
            "--boulder",
            str(boulder),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    restored = json.loads(boulder.read_text())
    assert restored["execution_work_id"] == row["execution_work_id"]
    assert restored["uv_bin"] == row["uv_bin"]


def _failure_duplicate_rows(root: Path) -> None:
    plan, ledger, plan_hash, row = _fixture(root)
    with ledger.open("ab") as stream:
        stream.write(_canonical(row) + b"\n")
    _expect_restore_failure(ledger, plan, plan_hash)


def _failure_revoked_reuse(root: Path) -> None:
    plan, ledger, plan_hash, row = _fixture(root)
    revoked = {"event": "work-revoked", "execution_work_id": row["execution_work_id"]}
    with ledger.open("ab") as stream:
        stream.write(_canonical(revoked) + b"\n")
    _expect_restore_failure(ledger, plan, plan_hash)


def _failure_corrupt_ledger(root: Path) -> None:
    plan, ledger, plan_hash, _row = _fixture(root)
    with ledger.open("ab") as stream:
        stream.write(b"{garbage}\n")
    _expect_restore_failure(ledger, plan, plan_hash)


def _failure_tool_hash_drift(root: Path) -> None:
    plan, ledger, plan_hash, row = _fixture(root)
    row["uv_sha256"] = "0" * 64
    ledger.write_bytes(_canonical(row) + b"\n")
    _expect_restore_failure(ledger, plan, plan_hash)


def _failure_relative_path(root: Path) -> None:
    plan, ledger, plan_hash, row = _fixture(root)
    row["plan_path"] = ".omo/plans/plan.md"
    ledger.write_bytes(_canonical(row) + b"\n")
    _expect_restore_failure(ledger, plan, plan_hash)


def _failure_symlink_escape(root: Path) -> None:
    pipeline = root / "pipeline"
    (pipeline / "services").mkdir(parents=True)
    outside = root / "outside.py"
    outside.write_text("secret\n")
    (pipeline / "services/escape.py").symlink_to(outside)
    contract = root / "contract.json"
    contract.write_text("{}")
    try:
        create_snapshot(pipeline, contract, root / "snapshot.json")
    except SnapshotError:
        return
    raise AssertionError("snapshot unexpectedly followed symlink")


def _failure_content_change_stable_id(root: Path) -> None:
    pipeline = root / "pipeline"
    (pipeline / "services").mkdir(parents=True)
    source = pipeline / "services/value.py"
    source.write_text("VALUE = 1\n")
    contract = root / "contract.json"
    contract.write_text("{}")
    first = root / "first.json"
    second = root / "second.json"
    create_snapshot(pipeline, contract, first)
    source.write_text("VALUE = 2\n")
    create_snapshot(pipeline, contract, second)
    first_value = json.loads(first.read_text())
    second_value = json.loads(second.read_text())
    assert first_value["execution_contract_sha256"] == second_value["execution_contract_sha256"]
    assert first_value["tree_sha256"] != second_value["tree_sha256"]


def _forbidden_imports(root: Path) -> tuple[str, ...]:
    forbidden = {"openai", "resolve", "whisper"}
    found: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(
                    alias.name
                    for alias in node.names
                    if alias.name.split(".")[0] in forbidden
                )
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.split(".")[0] in forbidden
            ):
                found.append(node.module)
    return tuple(found)


def _failure_premature_dependency(root: Path) -> None:
    services = Path.cwd() / "services"
    assert _forbidden_imports(services) == ()
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "forbidden.py").write_text("import openai\n")
    assert _forbidden_imports(fixture) == ("openai",)


CASES: dict[str, Callable[[Path], None]] = {
    "happy-workinit-reuse": _happy_workinit_reuse,
    "happy-boulder-delete-resume": _happy_boulder_delete_resume,
    "failure-duplicate-rows": _failure_duplicate_rows,
    "failure-revoked-reuse": _failure_revoked_reuse,
    "failure-corrupt-ledger": _failure_corrupt_ledger,
    "failure-tool-hash-drift": _failure_tool_hash_drift,
    "failure-relative-path": _failure_relative_path,
    "failure-symlink-escape": _failure_symlink_escape,
    "failure-content-change-stable-id": _failure_content_change_stable_id,
    "failure-premature-dependency": _failure_premature_dependency,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=tuple(CASES))
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix=f"todo-1-{arguments.case}-") as temporary:
        CASES[arguments.case](Path(temporary).resolve())
    print(f"PASS {arguments.case}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
