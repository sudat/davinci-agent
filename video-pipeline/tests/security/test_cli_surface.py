"""Attack class 12: every Todo-65 CLI surface attempted with side checks.

All 33 registered operations are enumerated and importable; every typed
refusal class (shell/path/network/UI/unknown) is actively attempted and
proven to spawn NOTHING; a registered dispatch passes an exact explicit
argv (never a shell string); and the dispatch surface has no UI or
network dependency reachable by name.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from services.cli import operator
from services.cli.operator import (
    OPERATIONS,
    REFUSAL_NETWORK,
    REFUSAL_PATH,
    REFUSAL_SHELL,
    REFUSAL_UI,
    REFUSAL_UNKNOWN,
    OperatorRefusalError,
    dispatch,
    help_text,
)

EXPECTED_OPERATIONS = (
    "ingest",
    "normalize",
    "conform",
    "conform-map",
    "phase1",
    "review",
    "checkpoint",
    "convert-review",
    "preview",
    "qc",
    "run-gate",
    "verify-policy",
    "gate-p1-faults",
    "gate-p2-faults",
    "gate-p3-faults",
    "freeze-phase",
    "materialize",
    "toolchain-verify",
    "mcp-doctor",
    "episode0",
    "episode-report",
    "toolchain-ledger",
    "retention-gc",
    "metrics-report",
    "kpi",
    "check-scope",
    "evidence-append",
    "evidence-verify",
    "preflight",
    "export-schemas",
    "qa-run-todo",
    "cockpit",
    "legacy-report",
)


def test_10_exactly_28_unique_registered_operations() -> None:
    names = tuple(op.name for op in OPERATIONS)
    assert len(names) == 33
    assert len(set(names)) == 33
    assert set(names) == set(EXPECTED_OPERATIONS)
    assert all(op.module.startswith("services.") for op in OPERATIONS)


def test_11_every_registered_module_is_importable() -> None:
    for op in OPERATIONS:
        module = importlib.import_module(op.module)
        assert hasattr(module, "main"), op.module


def test_12_help_lists_every_operation_and_the_refusal_contract() -> None:
    text = help_text()
    for op in OPERATIONS:
        assert op.name in text
    for code in (
        REFUSAL_SHELL,
        REFUSAL_PATH,
        REFUSAL_NETWORK,
        REFUSAL_UI,
        REFUSAL_UNKNOWN,
    ):
        assert code in text
    assert dispatch(["--help"]) == 0


@pytest.mark.parametrize(
    ("name", "code"),
    [
        *[(n, REFUSAL_SHELL) for n in ("sh", "bash", "zsh", "exec", "spawn", "eval")],
        *[(n, REFUSAL_PATH) for n in ("cat", "rm", "mv", "cp", "write", "open", "path")],
        *[(n, REFUSAL_NETWORK) for n in ("curl", "wget", "ssh", "fetch", "upload")],
        *[(n, REFUSAL_UI) for n in ("ui", "serve", "dashboard", "browser", "window")],
        ("sudo", REFUSAL_UNKNOWN),
        ("python", REFUSAL_UNKNOWN),
        ("os.system", REFUSAL_UNKNOWN),
        ("rm -rf /", REFUSAL_UNKNOWN),
        ("../../etc/passwd", REFUSAL_UNKNOWN),
        ("http://evil.example.invalid", REFUSAL_UNKNOWN),
    ],
)
def test_20_every_refusal_attempt_spawns_nothing(
    name: str, code: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_spawn(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"refused name {name!r} spawned a process")

    monkeypatch.setattr(operator.subprocess, "run", no_spawn)
    error = operator.refuse(name)
    assert isinstance(error, OperatorRefusalError)
    assert error.code == code
    assert dispatch([name]) == 2


def test_21_empty_argv_is_help_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_spawn(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("empty argv must not dispatch")

    monkeypatch.setattr(operator.subprocess, "run", no_spawn)
    assert dispatch([]) == 2


def test_30_dispatch_passes_explicit_argv_never_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(operator.subprocess, "run", fake_run)
    hostile_args = ["--target", "; rm -rf /", "$(whoami)", "`id`", "../../etc/passwd"]
    assert dispatch(["evidence-append", *hostile_args]) == 7
    assert captured == [[sys.executable, "-m", "services.evidence.append_event", *hostile_args]]


def test_40_operator_surface_has_no_ui_or_network_dependency() -> None:
    source = Path(operator.__file__).read_text(encoding="utf-8")
    for forbidden in ("tkinter", "PyQt", "webbrowser", "http.client", "urllib.request", "socket"):
        assert forbidden not in source
    assert "shell=True" not in source
