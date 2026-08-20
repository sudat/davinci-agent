"""Todo 65: the typed operator CLI surface (``python -m services.cli``).

``--help`` must list every registered pipeline operation and the exact
FINAL_APPROVED Global Review Report v1 contract. Dispatch reaches only the
fixed operation registry — arbitrary shell, path, network, or UI commands are
typed refusals, never executed.
"""

from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from pathlib import Path

from services.cli.operator import OPERATIONS

FINAL_CONTRACT_TOKENS = (
    "FINAL_APPROVED",
    "goal-constraints.json",
    "code-quality.json",
    "security.json",
    "hands-on-qa.json",
    "context-mining.json",
    "debugging.json",
    "APPROVE",
    "report-set",
    "TTY",
)

REFUSAL_CASES = (
    (["shell", "-c", "echo hi"], "arbitrary-shell-command"),
    (["exec", "rm", "-rf", "/"], "arbitrary-shell-command"),
    (["sh", "-c", "id"], "arbitrary-shell-command"),
    (["read-file", "/etc/passwd"], "arbitrary-path-command"),
    (["rm", "-rf", str(Path.cwd())], "arbitrary-path-command"),
    (["curl", "http://example.invalid"], "network-command"),
    (["wget", "http://example.invalid/x"], "network-command"),
    (["ui", "--port", "8080"], "ui-command"),
    (["serve", "--bind", "127.0.0.1"], "ui-command"),
)


def run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.cli", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
        timeout=120,
    )


def test_10_help_lists_every_registered_operation() -> None:
    result = run_cli(["--help"])
    assert result.returncode == 0
    for operation in OPERATIONS:
        assert operation.name in result.stdout, f"missing operation {operation.name}"


def test_20_help_states_the_exact_final_report_contract() -> None:
    result = run_cli(["--help"])
    assert result.returncode == 0
    for token in FINAL_CONTRACT_TOKENS:
        assert token in result.stdout, f"missing contract token {token}"


def test_30_dispatch_delegates_to_a_registered_module() -> None:
    result = run_cli(["checkpoint", "record", "--help"])
    assert result.returncode == 0
    assert "--purpose" in result.stdout
    delegated = run_cli(["metrics-report", "--help"])
    assert delegated.returncode == 0
    assert "--events" in delegated.stdout


def test_40_registry_modules_are_importable() -> None:
    for operation in OPERATIONS:
        assert import_module(operation.module) is not None


def test_50_arbitrary_commands_are_typed_refusals() -> None:
    for argv, code in REFUSAL_CASES:
        result = run_cli(argv)
        assert result.returncode == 2, f"{argv} must refuse"
        assert f"refused: {code}" in result.stderr, f"{argv} must cite {code}"


def test_52_unknown_operations_are_typed_refusals() -> None:
    result = run_cli(["definitely-not-an-operation"])
    assert result.returncode == 2
    assert "refused: unknown-operation" in result.stderr


def test_60_operator_surface_has_no_ui_dependency() -> None:
    offenders = [
        path.name
        for path in sorted(Path("services/cli").glob("*.py"))
        for token in ("tkinter", "PyQt", "streamlit", "flask", "gradio", "webview", "pygame")
        if token.lower() in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"UI dependencies are forbidden in the operator CLI: {offenders}"
