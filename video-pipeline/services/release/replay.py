"""Clean-room replay of the acceptance pipeline from a staged snapshot.

A fresh uv environment/cache is created under ``--out``; the only network
exit is a loopback-only guard that aborts any external egress attempt with a
typed marker (hidden network prevents release). Steps are checkpointed, so
an interrupted replay restarts safely and completed steps are reused.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.release.errors import ReleaseGateError
from services.release.extract_candidate import copy_tree
from services.release.manifest import tree_hash
from services.release.network_guard import (
    CACHE_DIR_NAME,
    NETWORK_MARKER,
    child_environment,
    write_network_guard,
)
from services.release.staging import sha256_bytes
from services.release.verify import verify_candidate

CLEAN_ROOM_NAME = "clean-room"
SEED_MARKER = ".release-cache-seeded"
STATE_NAME = "replay-state.json"
REPORT_NAME = "replay-report.json"
DEFAULT_PYTEST_ARGS: tuple[str, ...] = (
    "-q",
    "-p",
    "no:cacheprovider",
    "-m",
    "not resolve_live and not cloud_fixture",
)


class ReplayStep(StrictModel):
    name: str
    argv: tuple[str, ...]
    input_sha256: Sha256
    exit_code: int
    completed: bool
    reused: bool
    stdout_sha256: Sha256
    stderr_sha256: Sha256


class ReplayReport(StrictModel):
    schema_version: Literal["release-replay-v1"] = "release-replay-v1"
    source_kind: Literal["candidate", "staging", "extract"]
    source_tree_sha256: Sha256
    verdict: Literal["passed", "failed"]
    network_blocked: bool
    source_unmodified: bool
    steps: tuple[ReplayStep, ...]
    reused_steps: tuple[str, ...]


class ReplayState(StrictModel):
    schema_version: Literal["release-replay-state-v1"] = "release-replay-state-v1"
    source_tree_sha256: Sha256
    steps: tuple[ReplayStep, ...] = ()


def _input_hash(name: str, argv: tuple[str, ...], source_tree: str) -> str:
    payload = name + "\x00" + "\x00".join(argv) + "\x00" + source_tree
    return sha256_bytes(payload.encode())


def seed_cache(out: Path, seed: Path | None) -> None:
    cache = out / CACHE_DIR_NAME
    cache.mkdir(parents=True, exist_ok=True)
    if (cache / SEED_MARKER).exists():
        return
    if seed is not None:
        shutil.copytree(seed, cache, dirs_exist_ok=True, copy_function=os.link)
    (cache / SEED_MARKER).write_text(str(seed) if seed is not None else "empty")


def _run_step(
    out: Path,
    name: str,
    argv: tuple[str, ...],
    cwd: Path,
    source_tree: str,
    previous: ReplayStep | None,
) -> ReplayStep:
    expected = _input_hash(name, argv, source_tree)
    if previous is not None and previous.completed and previous.input_sha256 == expected:
        return previous.model_copy(update={"reused": True})
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=child_environment(out),
        check=False,
        capture_output=True,
        text=True,
    )
    logs = out / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{name}.out").write_text(result.stdout)
    (logs / f"{name}.err").write_text(result.stderr)
    combined = result.stdout + result.stderr
    if NETWORK_MARKER in combined:
        raise ReleaseGateError("hidden_network", f"step {name} attempted network egress")
    return ReplayStep(
        name=name,
        argv=argv,
        input_sha256=expected,
        exit_code=result.returncode,
        completed=result.returncode == 0,
        reused=False,
        stdout_sha256=sha256_bytes(result.stdout.encode()),
        stderr_sha256=sha256_bytes(result.stderr.encode()),
    )


def _prepare_step(
    source_root: Path,
    clean_source: Path,
    source_tree: str,
    previous: ReplayStep | None,
) -> ReplayStep:
    """In-process, checkpointed clean-room copy of the staged source."""

    argv = ("copy-source", str(source_root), str(clean_source))
    expected = _input_hash("prepare-clean-room", argv, source_tree)
    if (
        previous is not None
        and previous.completed
        and previous.input_sha256 == expected
        and clean_source.is_dir()
        and tree_hash(clean_source) == source_tree
    ):
        return previous.model_copy(update={"reused": True})
    if clean_source.exists():
        shutil.rmtree(clean_source)
    copied = copy_tree(source_root, clean_source)
    if not copied:
        raise ReleaseGateError("missing_release_input", f"empty source snapshot: {source_root}")
    return ReplayStep(
        name="prepare-clean-room",
        argv=argv,
        input_sha256=expected,
        exit_code=0,
        completed=True,
        reused=False,
        stdout_sha256=sha256_bytes(b""),
        stderr_sha256=sha256_bytes(b""),
    )


def run_replay(
    source_input: Path,
    source_kind: Literal["candidate", "staging", "extract"],
    out: Path,
    uv_bin: Path,
    seed: Path | None,
    pytest_args: tuple[str, ...],
) -> ReplayReport:
    """Replay the offline acceptance pipeline in a clean room."""

    if source_kind == "candidate":
        verify_candidate(
            source_input,
            expected_git_sha=None,
            require_h1_binding=True,
            recompute=True,
            require_readonly=True,
        )
    out.mkdir(parents=True, exist_ok=True)
    write_network_guard(out)
    source_root = source_input / "source"
    source_tree = tree_hash(source_root)
    state_path = out / STATE_NAME
    state = (
        ReplayState.model_validate_json(state_path.read_bytes())
        if state_path.is_file()
        else ReplayState(source_tree_sha256=source_tree)
    )
    done = {step.name: step for step in state.steps}
    clean_source = out / CLEAN_ROOM_NAME / "source"
    prepare = _prepare_step(source_root, clean_source, source_tree, done.get("prepare-clean-room"))
    seed_cache(out, seed)
    sync_argv: tuple[str, ...] = (str(uv_bin), "sync", "--frozen", "--offline")
    sync = _run_step(
        out,
        "sync-frozen-offline",
        sync_argv,
        clean_source,
        source_tree,
        done.get("sync-frozen-offline"),
    )
    acceptance_argv: tuple[str, ...] = (
        str(uv_bin),
        "run",
        "--frozen",
        "--offline",
        "--no-sync",
        "pytest",
        *pytest_args,
    )
    acceptance = _run_step(
        out,
        "acceptance-offline",
        acceptance_argv,
        clean_source,
        source_tree,
        done.get("acceptance-offline"),
    )
    source_unmodified = tree_hash(clean_source) == source_tree
    steps = (prepare, sync, acceptance)
    state = ReplayState(source_tree_sha256=source_tree, steps=steps)
    atomic_write(state_path, canonical_model_bytes(state))
    verdict: Literal["passed", "failed"] = (
        "passed"
        if all(step.completed for step in steps) and source_unmodified
        else "failed"
    )
    report = ReplayReport(
        source_kind=source_kind,
        source_tree_sha256=source_tree,
        verdict=verdict,
        network_blocked=False,
        source_unmodified=source_unmodified,
        steps=steps,
        reused_steps=tuple(step.name for step in steps if step.reused),
    )
    atomic_write(out / REPORT_NAME, canonical_model_bytes(report))
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidate", type=Path)
    source.add_argument("--staging", type=Path)
    source.add_argument("--candidate-extract", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--uv-bin", type=Path, required=True)
    parser.add_argument("--seed-cache", type=Path)
    parser.add_argument("--pytest-arg", action="append")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    pytest_args = tuple(arguments.pytest_arg) if arguments.pytest_arg else DEFAULT_PYTEST_ARGS
    source_input, source_kind = (
        (arguments.candidate, "candidate")
        if arguments.candidate is not None
        else (arguments.staging, "staging")
        if arguments.staging is not None
        else (arguments.candidate_extract, "extract")
    )
    try:
        report = run_replay(
            source_input,
            source_kind,
            arguments.out,
            arguments.uv_bin,
            arguments.seed_cache,
            pytest_args,
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(canonical_model_bytes(report).decode())
    return 0 if report.verdict == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PYTEST_ARGS",
    "NETWORK_MARKER",
    "ReplayReport",
    "ReplayState",
    "ReplayStep",
    "run_replay",
    "seed_cache",
    "write_network_guard",
]
