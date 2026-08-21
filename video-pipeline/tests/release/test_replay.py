"""Clean-room replay: fresh env/cache, offline guard, restart safety."""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from services.release.network_guard import NETWORK_MARKER, child_environment, write_network_guard
from services.release.replay import (
    DEFAULT_PYTEST_ARGS,
    WORKSPACE_ANCHORED_IGNORES,
    run_replay,
    snapshot_tree_hash,
)
from services.release.replay import (
    main as replay_main,
)
from tests.release.support import build_test_candidate, expect_gate_error

STUB_UV = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "\n"
    "argv = sys.argv[1:]\n"
    'if "--network" in argv:\n'
    "    import socket\n"
    "\n"
    '    socket.create_connection(("93.184.216.34", 80), timeout=1)\n'
    "    sys.exit(9)\n"
    'if argv[:1] == ["sync"]:\n'
    "    sys.exit(0)\n"
    'if "pytest" in argv:\n'
    '    print("3 passed")\n'
    "    sys.exit(0)\n"
    "sys.exit(1)\n"
)


def make_stub_uv(tmp: Path) -> Path:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(STUB_UV)
    stub.chmod(0o755)
    return stub


def uv_sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_failing_uv(tmp: Path) -> Path:
    stub = tmp / "bin-fail" / "uv"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    return stub


def test_10_happy_replay_fresh_env_no_network(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    out = tmp_path / "replay"
    stub = make_stub_uv(tmp_path)
    report = run_replay(candidate, "candidate", out, stub, None, ("-q",), uv_sha256=uv_sha_of(stub))
    assert report.verdict == "passed"
    assert report.source_unmodified is True
    assert report.network_blocked is False
    assert all(step.completed and step.exit_code == 0 for step in report.steps)
    assert (out / "uv-cache" / ".release-cache-seeded").is_file()
    assert (out / "network-guard" / "sitecustomize.py").is_file()
    assert (out / "replay-state.json").is_file()
    assert (out / "replay-report.json").is_file()
    assert (out / "clean-room" / "source" / "pyproject.toml").is_file()
    subprocess_steps = [step for step in report.steps if step.name != "prepare-clean-room"]
    assert all(step.argv[0] == str(stub) for step in subprocess_steps)
    prepare = next(step for step in report.steps if step.name == "prepare-clean-room")
    assert prepare.argv[0] == "copy-source"


def test_11_restart_second_run_reuses_completed_steps(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    out = tmp_path / "replay"
    stub = make_stub_uv(tmp_path)
    first = run_replay(candidate, "candidate", out, stub, None, ("-q",), uv_sha256=uv_sha_of(stub))
    second = run_replay(candidate, "candidate", out, stub, None, ("-q",), uv_sha256=uv_sha_of(stub))
    assert first.verdict == second.verdict == "passed"
    assert second.reused_steps == tuple(step.name for step in second.steps)


def test_12_crash_recovery_reruns_incomplete_step(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    out = tmp_path / "replay"
    stub = make_stub_uv(tmp_path)
    run_replay(candidate, "candidate", out, stub, None, ("-q",), uv_sha256=uv_sha_of(stub))
    state_path = out / "replay-state.json"
    state_path.write_bytes(
        state_path.read_bytes().replace(b'"completed":true', b'"completed":false', 1)
    )
    report = run_replay(candidate, "candidate", out, stub, None, ("-q",), uv_sha256=uv_sha_of(stub))
    assert report.verdict == "passed"
    assert "prepare-clean-room" not in report.reused_steps
    assert "acceptance-offline" in report.reused_steps


def test_20_hidden_network_prevents_release(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    expect_gate_error(
        "hidden_network",
        lambda: run_replay(
            candidate, "candidate", tmp_path / "replay", stub, None, ("--network",),
            uv_sha256=uv_sha_of(stub),
        ),
    )


def test_21_failed_acceptance_is_failed_verdict(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    failing = make_failing_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay", failing, None, ("-q",),
        uv_sha256=uv_sha_of(failing),
    )
    assert report.verdict == "failed"


def test_30_guard_blocks_external_and_allows_loopback(tmp_path: Path) -> None:
    out = tmp_path / "guard-test"
    out.mkdir()
    write_network_guard(out)
    guard_env = {**os.environ, "PYTHONPATH": str(out / "network-guard")}
    blocked_probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 80), timeout=1)\n"
        "    print('CONNECTED')\n"
        "except SystemExit as e:\n"
        "    print('BLOCKED', e)\n"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", blocked_probe],
        env=guard_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert NETWORK_MARKER in blocked.stdout
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        loopback_probe = (
            "import socket\n"
            f"s = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
            "print('LOOPBACK OK')\n"
            "s.close()\n"
        )
        allowed = subprocess.run(
            [sys.executable, "-c", loopback_probe],
            env=guard_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "LOOPBACK OK" in allowed.stdout, allowed.stderr
    finally:
        server.close()


def test_40_cli_exit_codes(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    out = tmp_path / "replay"
    rc = replay_main(
        [
            "--candidate", str(candidate), "--out", str(out), "--uv-bin", str(stub),
            "--uv-sha256", uv_sha_of(stub), "--pytest-arg=-q", "--diagnostic",
        ]
    )
    assert rc == 0
    failing = make_failing_uv(tmp_path)
    rc = replay_main(
        [
            "--candidate", str(candidate), "--out", str(out / "f"),
            "--uv-bin", str(failing), "--uv-sha256", uv_sha_of(failing),
        ]
    )
    assert rc == 1
    rc = replay_main(
        [
            "--candidate", str(candidate), "--out", str(out / "g"),
            "--uv-bin", str(stub), "--uv-sha256", uv_sha_of(stub), "--pytest-arg=-q",
        ]
    )
    assert rc == 2


def test_41_offline_environment_is_enforced(tmp_path: Path) -> None:
    environment = child_environment(tmp_path)
    assert environment["UV_OFFLINE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONPATH"].endswith("network-guard")


def test_42_default_selection_excludes_only_workspace_anchored_modules() -> None:
    ignores = [item for item in DEFAULT_PYTEST_ARGS if item.startswith("--ignore=")]
    assert ignores == [f"--ignore={name}" for name in WORKSPACE_ANCHORED_IGNORES]
    assert "not resolve_live and not cloud_fixture" in DEFAULT_PYTEST_ARGS
    assert "no:cacheprovider" in DEFAULT_PYTEST_ARGS
    assert len(WORKSPACE_ANCHORED_IGNORES) == 7


def test_43_snapshot_hash_ignores_transient_artifacts_only(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("VALUE = 1\n")
    baseline = snapshot_tree_hash(source)
    (source / ".hypothesis").mkdir()
    (source / ".hypothesis" / "constants").write_text("{}")
    pycache = source / "pkg" / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00")
    assert snapshot_tree_hash(source) == baseline
    (source / "pkg" / "mod.py").write_text("VALUE = 2\n")
    assert snapshot_tree_hash(source) != baseline


@pytest.mark.parametrize("kind", ["staging", "extract"])
def test_50_staging_and_extract_kinds_accepted(tmp_path: Path, kind: str) -> None:
    _repo, _candidate, _sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    source = tmp_path / "plain-tree"
    (source / "source").mkdir(parents=True)
    (source / "source" / "pyproject.toml").write_text("[project]\nname='x'\n")
    report = run_replay(
        source,
        kind,  # type: ignore[arg-type]
        tmp_path / "replay",
        stub,
        None,
        ("-q",),
        uv_sha256=uv_sha_of(stub),
    )
    assert report.verdict == "passed"
