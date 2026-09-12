"""Repo-default editorial runtime fallback (codex fix B).

Given no explicit arg and no ``EDITORIAL_RUNTIME_CONFIG`` env, the gate
resolves the shipped ``config/editorial-runtime.json`` (production_model
+ codex-exec); explicit arg and env keep their override precedence.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from services.cli import episode_runner_editorial as gate


def _write_runtime(path: Path, mode: str) -> Path:
    path.write_text(json.dumps({"schema_version": "editorial-runtime-v1", "mode": mode}))
    return path


def test_absent_arg_and_env_resolves_repo_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(gate.EDITORIAL_RUNTIME_ENV, raising=False)
    log = io.BytesIO()
    assert gate.editorial_mode(None, log) == "production_model"
    assert gate.editorial_transport(None, log) == "codex-exec"


def test_explicit_arg_beats_repo_default(tmp_path: Path) -> None:
    heuristic = _write_runtime(tmp_path / "heuristic.json", "heuristic_diagnostic")
    log = io.BytesIO()
    assert gate.editorial_mode(heuristic, log) == "heuristic_diagnostic"


def test_env_beats_repo_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heuristic = _write_runtime(tmp_path / "heuristic.json", "heuristic_diagnostic")
    monkeypatch.setenv(gate.EDITORIAL_RUNTIME_ENV, str(heuristic))
    log = io.BytesIO()
    assert gate.editorial_mode(None, log) == "heuristic_diagnostic"
