"""Blocker-fix regression: the staging writer lock is an OS flock.

The old presence-based lock let a second invocation unlink a live
writer's lock whenever a staging tree existed. The fixed lock is an
exclusive ``flock`` held for the whole stage/seal lifetime: a live
writer blocks any second invocation; a crashed writer's leftover file
is safely retaken because the kernel released its flock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from services.release.build_candidate import (
    WriterLock,
    lock_path,
    staging_path,
)
from services.release.build_candidate import (
    main as build_main,
)
from services.release.errors import ReleaseGateError
from tests.release.support import (
    make_attempt_dir,
    make_git_repo,
    make_ledgers,
    make_plan,
)


def _args(tmp_path: Path, repo: Path, sha: str, candidate: Path) -> list[str]:
    start_work, execution = make_ledgers(tmp_path)
    return [
        "--repo", str(repo),
        "--git-sha", sha,
        "--out", str(candidate),
        "--plan", str(make_plan(tmp_path)),
        "--start-work-ledger", str(start_work),
        "--execution-ledger", str(execution),
        "--attempt-dir", str(make_attempt_dir(tmp_path)),
        "--receipt", str(tmp_path / "receipt.json"),
    ]


def test_live_flock_blocks_second_stage_invocation(tmp_path: Path) -> None:
    _repo, _sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    lock_path(candidate).parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path(candidate), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        staging_path(candidate).mkdir(parents=True)
        lock = WriterLock(candidate)
        with pytest.raises(ReleaseGateError, match="another writer"):
            lock.acquire()
    finally:
        os.close(descriptor)


def test_crashed_writer_lock_is_retaken_without_unlink(tmp_path: Path) -> None:
    _repo, _sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    lock_path(candidate).parent.mkdir(parents=True, exist_ok=True)
    # A crashed writer leaves the file behind; no flock is held anymore.
    lock_path(candidate).write_text("crashed")
    staging_path(candidate).mkdir(parents=True)
    lock = WriterLock(candidate)
    lock.acquire()
    try:
        assert lock_path(candidate).is_file()
    finally:
        lock.release()
    assert not lock_path(candidate).exists()


def test_seal_requires_staging_and_refuses_live_holder(tmp_path: Path) -> None:
    _repo, _sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    lock_path(candidate).parent.mkdir(parents=True, exist_ok=True)
    lock_path(candidate).write_text("stage-ran")
    staging_path(candidate).mkdir(parents=True)
    descriptor = os.open(lock_path(candidate), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with pytest.raises(ReleaseGateError, match="another writer"):
            WriterLock(candidate, create=False).acquire()
    finally:
        os.close(descriptor)


def test_seal_refuses_missing_stage_lock_file(tmp_path: Path) -> None:
    candidate = tmp_path / "cand"
    lock_path(candidate).parent.mkdir(parents=True, exist_ok=True)
    staging_path(candidate).mkdir(parents=True)
    with pytest.raises(ReleaseGateError, match="seal requires the stage-phase writer lock"):
        WriterLock(candidate, create=False).acquire()


def test_stage_then_seal_holds_one_lock_lifecycle(tmp_path: Path) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = _args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    assert staging_path(candidate).is_dir()
    assert lock_path(candidate).is_file()
    token = (staging_path(candidate) / ".staging-ownership.json")
    assert token.is_file()
    import json  # noqa: PLC0415

    build_token = json.loads(token.read_text())["build_token"]
    assert build_main([*args, "--phase", "seal", "--build-token", build_token]) == 0
    assert candidate.is_dir()
    assert not lock_path(candidate).exists()
