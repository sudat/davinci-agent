"""Candidate build: staging inputs, clean-tree gate, publish, chmod, recompute."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from services.release.build_candidate import (
    chmod_readonly,
    lock_path,
    staging_path,
)
from services.release.build_candidate import (
    main as build_main,
)
from services.release.staging import start_work_ledger_suffix
from tests.release.support import (
    build_test_candidate,
    commit_more,
    make_attempt_dir,
    make_git_repo,
    make_ledgers,
    make_plan,
)


def build_args(tmp: Path, repo: Path, sha: str, candidate: Path) -> list[str]:
    start_work, execution = make_ledgers(tmp)
    return [
        "--repo",
        str(repo),
        "--git-sha",
        sha,
        "--out",
        str(candidate),
        "--plan",
        str(make_plan(tmp)),
        "--start-work-ledger",
        str(start_work),
        "--execution-ledger",
        str(execution),
        "--attempt-dir",
        str(make_attempt_dir(tmp)),
        "--receipt",
        str(tmp / "receipt.json"),
    ]


def stage_rc(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = build_main(args)
    captured = capsys.readouterr()
    return code, captured.err


def test_10_full_build_layout_publishes_readonly(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    assert (candidate / "manifest.json").is_file()
    assert (candidate / "identity.json").is_file()
    assert (candidate / "source" / "pyproject.toml").is_file()
    assert (candidate / "inputs" / "plan.md").is_file()
    assert (candidate / "inputs" / "start-work-ledger.jsonl").is_file()
    assert (candidate / "inputs" / "execution-ledger.jsonl").is_file()
    assert (candidate / "inputs" / "gates" / "phase-0a" / "gate-result.json").is_file()
    assert (candidate / "inputs" / "h1" / "binding.json").is_file()
    assert (candidate / "inputs" / "h1" / "checkpoint-result.json").is_file()
    evidence66 = candidate / "inputs" / "task-evidence"
    assert (evidence66 / "task-66-foundation-video-pipeline.json").is_file()
    assert not (evidence66 / "task-67-foundation-video-pipeline.json").exists()
    assert not lock_path(candidate).exists()
    assert not staging_path(candidate).exists()
    assert (tmp_path / "receipt.json").is_file()
    for directory, _, files in os.walk(candidate):
        assert stat.S_IMODE(Path(directory).lstat().st_mode) & 0o222 == 0
        for name in files:
            assert stat.S_IMODE((Path(directory) / name).lstat().st_mode) & 0o222 == 0


def test_11_ledger_suffix_drops_todo67_rows(tmp_path: Path) -> None:
    start_work, _ = make_ledgers(tmp_path)
    suffix = start_work_ledger_suffix(start_work)
    assert b"todo-66" in suffix
    assert b"todo-67" not in suffix


def test_20_untracked_smuggle_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, sha = make_git_repo(tmp_path)
    (repo / "video-pipeline" / "rogue.py").write_text("x = 1\n")
    candidate = tmp_path / "cand"
    args = [*build_args(tmp_path, repo, sha, candidate), "--phase", "stage"]
    code, err = stage_rc(args, capsys)
    assert code == 2
    assert "untracked smuggle" in err
    assert not candidate.exists()


def test_21_preexisting_root_claude_tolerated_makefile_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha = make_git_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("notes\n")
    (repo / "Makefile").write_text("all:\n")
    candidate = tmp_path / "cand"
    args = [*build_args(tmp_path, repo, sha, candidate), "--phase", "stage"]
    code, err = stage_rc(args, capsys)
    assert code == 2
    assert "untracked smuggle" in err
    assert "Makefile" in err

    (repo / "Makefile").unlink()
    assert build_main([*build_args(tmp_path, repo, sha, candidate), "--phase", "all"]) == 0
    assert not (candidate / "source" / "CLAUDE.md").exists()


def test_22_dirty_worktree_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, sha = make_git_repo(tmp_path)
    (repo / "video-pipeline" / "services" / "core.py").write_text("VALUE = 9\n")
    args = [*build_args(tmp_path, repo, sha, tmp_path / "cand"), "--phase", "stage"]
    code, err = stage_rc(args, capsys)
    assert code == 2
    assert "not clean" in err


def test_23_wrong_sha_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _sha = make_git_repo(tmp_path)
    commit_more(repo)
    args = [*build_args(tmp_path, repo, "0" * 40, tmp_path / "cand"), "--phase", "stage"]
    code, err = stage_rc(args, capsys)
    assert code == 2
    assert "HEAD" in err


def test_24_missing_gate_evidence_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha = make_git_repo(tmp_path)
    args = build_args(tmp_path, repo, sha, tmp_path / "cand")
    (tmp_path / "attempt" / "phase-0a" / "gate-result.json").unlink()
    code, err = stage_rc([*args, "--phase", "stage"], capsys)
    assert code == 2
    assert "missing gate" in err


def test_30_duplicate_writer_on_existing_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, candidate, sha = build_test_candidate(tmp_path)
    args = [*build_args(tmp_path, repo, sha, candidate), "--phase", "all"]
    code, err = stage_rc(args, capsys)
    assert code == 2
    assert "already exists" in err


def test_31_duplicate_writer_on_held_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl  # noqa: PLC0415
    import os  # noqa: PLC0415

    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    lock_path(candidate).parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path(candidate), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        args = [*build_args(tmp_path, repo, sha, candidate), "--phase", "stage"]
        code, err = stage_rc(args, capsys)
        assert code == 2
        assert "another writer" in err
    finally:
        os.close(descriptor)


def test_40_stage_then_replay_restarts_safely_then_seal(tmp_path: Path) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = build_args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    assert staging_path(candidate).is_dir()
    second = build_main([*args, "--phase", "stage"])
    assert second == 0
    assert staging_path(candidate).is_dir()
    assert (staging_path(candidate) / ".staging-ownership.json").is_file()


def test_41_seal_requires_build_token(tmp_path: Path, capsys) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = build_args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    code = build_main([*args, "--phase", "seal"])
    assert code == 2
    assert "staging-ownership-required" in capsys.readouterr().err
    assert not candidate.exists()


def test_42_seal_with_foreign_staging_token_refused(tmp_path: Path, capsys) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = build_args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    code = build_main([*args, "--phase", "seal", "--build-token", "f" * 64])
    assert code == 2
    assert "staging-ownership-mismatch" in capsys.readouterr().err
    assert not candidate.exists()
    assert staging_path(candidate).is_dir()


def test_43_seal_refuses_staging_drift_after_stage(tmp_path: Path, capsys) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = build_args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    (staging_path(candidate) / "rogue.txt").write_text("drift\n")
    captured = capsys.readouterr()
    del captured
    from services.release.build_candidate import main as rebuild_main  # noqa: PLC0415

    code = rebuild_main(
        [*args, "--phase", "seal", "--build-token", "0" * 64]
    )
    assert code == 2
    assert "staging-ownership-mismatch" in capsys.readouterr().err


def test_44_stage_then_seal_with_printed_token_succeeds(
    tmp_path: Path, capsys
) -> None:
    repo, sha = make_git_repo(tmp_path)
    candidate = tmp_path / "cand"
    args = build_args(tmp_path, repo, sha, candidate)
    assert build_main([*args, "--phase", "stage"]) == 0
    token_line = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("staging-token:")
    )
    token = token_line.split("staging-token:", 1)[1].strip()
    assert len(token) >= 32
    assert build_main([*args, "--phase", "seal", "--build-token", token]) == 0
    assert candidate.is_dir()
    assert not lock_path(candidate).exists()
    assert not (candidate / ".staging-ownership.json").exists()


def test_50_chmod_readonly_is_bottom_up_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("x")
    chmod_readonly(root)
    chmod_readonly(root)
    assert stat.S_IMODE((root / "sub" / "f.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE((root / "sub").stat().st_mode) == 0o555
    assert stat.S_IMODE(root.stat().st_mode) == 0o555


def test_60_post_chmod_manifest_recompute_is_byte_identical(tmp_path: Path) -> None:
    from services.release.manifest import build_manifest, manifest_bytes  # noqa: PLC0415

    _repo, candidate, _sha = build_test_candidate(tmp_path)
    staged = (candidate / "manifest.json").read_bytes()
    assert manifest_bytes(build_manifest(candidate)) == staged
