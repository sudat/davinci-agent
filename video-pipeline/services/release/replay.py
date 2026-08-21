"""Clean-room replay of the acceptance pipeline from a staged snapshot.

A fresh uv environment/cache is created under ``--out``; the only network
exit is a loopback-only guard that aborts any external egress attempt with a
typed marker (hidden network prevents release). Steps are checkpointed, so
an interrupted replay restarts safely and completed steps are reused.
The ``--uv-bin`` executable is hash-verified against the pinned toolchain
lock (or an explicit ``--uv-sha256``) BEFORE any use, ``--pytest-arg`` is
restricted to explicitly-marked diagnostic runs, and the acceptance step
must report an executed pytest count at or above the floor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final, Literal

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.release.errors import ReleaseGateError
from services.release.extract_candidate import copy_tree
from services.release.network_guard import (
    CACHE_DIR_NAME,
    GUARD_LIMITATIONS,
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
# Transient interpreter/test-runner artifacts (gitignored by the repository):
# replaying the suite legitimately materializes them inside the clean room, so
# source integrity is compared over tracked-source content only.
TRANSIENT_NAMES = frozenset(
    {".DS_Store", ".hypothesis", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
# The acceptance-representative offline selection. Workspace-anchored modules
# are excluded because they bind evidence to the live attempt layout by
# design and cannot pass from any snapshot extract: runbook execution and
# ledger self-restore resolve ``.omo/start-work`` from ``cwd.parent`` (Todo 65
# runbooks, Todo 1 self-restore), and freeze-receipt policy verification
# compares absolute paths recorded at freeze time (Todos 6/24/32/47/56).
# Those evidence bindings are verified against the real attempt directory by
# F1, not by clean-room replay of the source snapshot.
WORKSPACE_ANCHORED_IGNORES: tuple[str, ...] = (
    "tests/docs/test_runbooks.py",
    "tests/gates/test_control_plane_inputs.py",
    "tests/gates/test_phase0c_policy.py",
    "tests/gates/test_phase1_inputs.py",
    "tests/gates/test_phase2_fault_cli.py",
    "tests/gates/test_phase2_inputs.py",
    "tests/gates/test_phase3_inputs.py",
    "tests/phase0a/test_fixture_contract.py",
)
DEFAULT_PYTEST_ARGS: tuple[str, ...] = (
    "-q",
    "-p",
    "no:cacheprovider",
    "-m",
    "not resolve_live and not cloud_fixture",
    *(f"--ignore={name}" for name in WORKSPACE_ANCHORED_IGNORES),
)
MIN_PASSED_FLOOR: Final = 1
PASSED_COUNT_RE: Final = re.compile(r"\b(\d+) passed\b")
TOOLCHAIN_LOCK_CANDIDATES: Final = (
    "phase-0a-v1.json",
    "phase-0b-v1.json",
    "phase-0c-v1.json",
    "phase-1-technical-v1.json",
    "phase-2-v1.json",
    "phase-3-v1.json",
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
    tests_passed: int | None = None


class ReplayReport(StrictModel):
    schema_version: Literal["release-replay-v1"] = "release-replay-v1"
    source_kind: Literal["candidate", "staging", "extract"]
    source_tree_sha256: Sha256
    verdict: Literal["passed", "failed", "diagnostic-passed"]
    verdict_scope: Literal["acceptance", "diagnostic"]
    network_blocked: bool
    source_unmodified: bool
    steps: tuple[ReplayStep, ...]
    reused_steps: tuple[str, ...]
    guard_limitations: str = GUARD_LIMITATIONS


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
    passed_match = PASSED_COUNT_RE.search(result.stdout)
    return ReplayStep(
        name=name,
        argv=argv,
        input_sha256=expected,
        exit_code=result.returncode,
        completed=result.returncode == 0,
        reused=False,
        stdout_sha256=sha256_bytes(result.stdout.encode()),
        stderr_sha256=sha256_bytes(result.stderr.encode()),
        tests_passed=(
            int(passed_match.group(1)) if name == "acceptance-offline" and passed_match else None
        ),
    )


def _pinned_uv_sha256(source_root: Path) -> str | None:
    """The pinned uv hash from the frozen toolchain lock beside the source."""
    for name in TOOLCHAIN_LOCK_CANDIDATES:
        lock_path = source_root / "config" / "toolchains" / name
        if not lock_path.is_file():
            continue
        try:
            lock = json.loads(lock_path.read_text())
        except ValueError:
            continue
        record = lock.get("python", {}).get("uv", {})
        sha256 = record.get("sha256") if isinstance(record, dict) else None
        if isinstance(sha256, str):
            return sha256
    return None


def _verify_uv_bin(uv_bin: Path, uv_sha256: str | None, source_root: Path) -> str:
    if uv_sha256 is None:
        uv_sha256 = _pinned_uv_sha256(source_root)
    if uv_sha256 is None:
        raise ReleaseGateError(
            "uv-sha-required",
            "replay requires the pinned uv hash (explicit --uv-sha256 or the "
            "frozen toolchain lock beside the source)",
        )
    if not uv_bin.is_file():
        raise ReleaseGateError("missing_release_input", f"uv binary not found: {uv_bin}")
    actual = sha256_file(uv_bin)
    if actual != uv_sha256:
        raise ReleaseGateError(
            "uv-hash-mismatch",
            f"uv binary {uv_bin} hashes {actual} != pinned {uv_sha256}",
        )
    return actual


def snapshot_tree_hash(root: Path) -> str:
    """Tree hash over tracked-source content (transient artifacts skipped)."""

    digest = hashlib.sha256()
    stack = [root]
    files: list[str] = []
    while stack:
        current = stack.pop()
        with os.scandir(current) as iterator:
            for entry in sorted(iterator, key=lambda item: item.name):
                if entry.name in TRANSIENT_NAMES:
                    continue
                if entry.is_symlink():
                    raise ReleaseGateError("stale_hash", f"symlink in clean room: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                relative = Path(entry.path).relative_to(root).as_posix()
                digest_line = f"{relative}\x00{sha256_file(Path(entry.path))}\n"
                files.append(digest_line)
    for line in sorted(files):
        digest.update(line.encode())
    return digest.hexdigest()


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
        and snapshot_tree_hash(clean_source) == source_tree
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
    *,
    uv_sha256: str | None = None,
    min_passed: int = 1,
    diagnostic: bool = False,
) -> ReplayReport:
    """Replay the offline acceptance pipeline in a clean room.

    ``diagnostic=True`` marks the run as a diagnostic: the report carries
    ``verdict_scope="diagnostic"`` and a passing diagnostic NEVER uses the
    acceptance verdict ``"passed"`` (it reports ``"diagnostic-passed"``).
    ``min_passed`` must be a positive integer and can only RAISE the
    non-overridable acceptance floor ``MIN_PASSED_FLOOR``.
    """

    if min_passed < 1:
        raise ReleaseGateError(
            "min-passed-invalid",
            f"--min-passed must be a positive integer, got {min_passed}",
        )
    effective_min_passed = max(min_passed, MIN_PASSED_FLOOR)
    if source_kind == "candidate":
        verify_candidate(
            source_input,
            expected_git_sha=None,
            require_h1_binding=True,
            recompute=True,
            require_readonly=True,
        )
    _verify_uv_bin(uv_bin, uv_sha256, source_input / "source")
    out.mkdir(parents=True, exist_ok=True)
    write_network_guard(out)
    source_root = source_input / "source"
    source_tree = snapshot_tree_hash(source_root)
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
    source_unmodified = snapshot_tree_hash(clean_source) == source_tree
    steps = (prepare, sync, acceptance)
    state = ReplayState(source_tree_sha256=source_tree, steps=steps)
    atomic_write(state_path, canonical_model_bytes(state))
    acceptance_proven = acceptance.completed and acceptance.tests_passed is not None
    if acceptance.completed and not acceptance_proven:
        acceptance_proven = False
    scope: Literal["acceptance", "diagnostic"] = (
        "diagnostic" if diagnostic else "acceptance"
    )
    verdict: Literal["passed", "failed", "diagnostic-passed"]
    gates_ok = source_unmodified and all(
        step.completed for step in steps if step.name != "acceptance-offline"
    )
    if (
        not acceptance_proven
        or (acceptance.tests_passed or 0) < effective_min_passed
        or not gates_ok
    ):
        verdict = "failed"
    elif diagnostic:
        verdict = "diagnostic-passed"
    else:
        verdict = "passed"
    report = ReplayReport(
        source_kind=source_kind,
        source_tree_sha256=source_tree,
        verdict=verdict,
        verdict_scope=scope,
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
    parser.add_argument("--uv-sha256")
    parser.add_argument("--seed-cache", type=Path)
    parser.add_argument("--pytest-arg", action="append")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--min-passed", type=int, default=1)
    parser.add_argument("--h1-binding", type=Path)
    parser.add_argument("--inject")
    parser.add_argument("--profile-swap")
    return parser


def _run_live(arguments: argparse.Namespace) -> int:
    """Live fault-injecting replay (Todo 67 / F3 contract)."""

    from services.foundation_io import sha256_file as hash_file  # noqa: PLC0415
    from services.release.live_flow import (  # noqa: PLC0415
        complete_invocation,
        parse_injections,
        parse_profiles,
        revalidate_live_replay,
        run_live_replay,
    )
    from services.release.live_guard import verify_h1_binding  # noqa: PLC0415
    from services.release.live_wiring import (  # noqa: PLC0415
        pinned_media_bins,
        production_seams,
        resolve_host_report,
    )
    from services.release.manifest import candidate_id  # noqa: PLC0415
    from services.release.verify import read_manifest  # noqa: PLC0415

    try:
        injections = parse_injections(arguments.inject)
        profiles = parse_profiles(arguments.profile_swap)
        host_report = resolve_host_report(arguments.out)
        ffmpeg, ffprobe = pinned_media_bins()
        seams = production_seams(
            host_report_path=host_report,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            out_root=arguments.out,
        )
        h1 = verify_h1_binding(arguments.candidate_extract, arguments.h1_binding)
        manifest, _payload = read_manifest(arguments.candidate_extract)
        invocation = complete_invocation(
            candidate_id=candidate_id(manifest),
            git_sha=h1.git_sha,
            h1_binding_sha256=h1.binding_sha256,
            injections=injections,
            profiles=profiles,
            tool_sha256=(hash_file(ffmpeg), hash_file(ffprobe)),
        )
    except Exception as error:  # noqa: BLE001 (typed usage failure, never a silent pass)
        print(f"live replay inputs invalid: {error}", file=sys.stderr)
        return 2
    revalidated = revalidate_live_replay(arguments.out, seams=seams, invocation=invocation)
    if revalidated is not None:
        print(canonical_model_bytes(revalidated).decode())
        print("live-replay: revalidated (idempotent re-run)")
        return 0
    summary = run_live_replay(
        extract=arguments.candidate_extract,
        h1_binding=arguments.h1_binding,
        injections=injections,
        profiles=profiles,
        out=arguments.out,
        seams=seams,
        tool_sha256=invocation.tool_sha256,
    )
    print(canonical_model_bytes(summary).decode())
    return 0 if summary.verdict in ("passed", "diagnostic-passed") else 1


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    live_flags = (arguments.h1_binding, arguments.inject, arguments.profile_swap)
    if any(flag is not None for flag in live_flags):
        if arguments.candidate_extract is None or any(flag is None for flag in live_flags):
            print(
                "live replay requires --candidate-extract together with "
                "--h1-binding, --inject and --profile-swap",
                file=sys.stderr,
            )
            return 2
        return _run_live(arguments)
    if arguments.pytest_arg and not arguments.diagnostic:
        print(
            "--pytest-arg is restricted to explicitly-marked diagnostic runs; "
            "release acceptance uses the fixed default selection",
            file=sys.stderr,
        )
        return 2
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
            uv_sha256=arguments.uv_sha256,
            min_passed=arguments.min_passed,
            diagnostic=arguments.diagnostic,
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(canonical_model_bytes(report).decode())
    return 0 if report.verdict in ("passed", "diagnostic-passed") else 1


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
