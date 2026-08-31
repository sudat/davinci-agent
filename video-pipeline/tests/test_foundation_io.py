"""Locks repo_root()'s observable contract before the CLI-local duplicates are removed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from services import foundation_io
from services.foundation_io import repo_root

_GIT_TOPLEVEL = ["git", "rev-parse", "--show-toplevel"]


def test_repo_root_returns_git_toplevel() -> None:
    probe = subprocess.run(
        _GIT_TOPLEVEL,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if probe.returncode != 0:
        pytest.skip("git toplevel unavailable in this environment")
    assert repo_root() == Path(probe.stdout.strip())


def test_repo_root_falls_back_when_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("git")

    monkeypatch.setattr(foundation_io.subprocess, "run", fake_run)
    assert repo_root() == Path(__file__).resolve().parents[2]


def test_repo_root_falls_back_when_git_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(cmd=_GIT_TOPLEVEL, timeout=5)

    monkeypatch.setattr(foundation_io.subprocess, "run", fake_run)
    assert repo_root() == Path(__file__).resolve().parents[2]


def test_repo_root_falls_back_when_git_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=_GIT_TOPLEVEL, returncode=128, stdout="", stderr="fatal: not a git repository"
        )

    monkeypatch.setattr(foundation_io.subprocess, "run", fake_run)
    assert repo_root() == Path(__file__).resolve().parents[2]
