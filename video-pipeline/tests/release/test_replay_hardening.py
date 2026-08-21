"""Blocker-fix regression: replay acceptance hardening + guard env hygiene.

--uv-bin must hash-match the pinned toolchain lock before use; --pytest-arg
is diagnostic-only; the acceptance step must prove a real pytest summary
count; and the replay child environment is a minimal allowlist that strips
credentials instead of inheriting the parent env.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.release.errors import ReleaseGateError
from services.release.network_guard import GUARD_LIMITATIONS, child_environment
from services.release.replay import run_replay
from tests.release.support import build_test_candidate
from tests.release.test_replay import make_failing_uv, make_stub_uv

PASSED_ARG_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "argv = sys.argv[1:]\n"
    'if argv[:1] == ["sync"]:\n'
    "    sys.exit(0)\n"
    'if "pytest" in argv:\n'
    '    print("1234 passed, 7 deselected in 9.99s")\n'
    "    sys.exit(0)\n"
    "sys.exit(1)\n"
)


def _counting_uv(tmp: Path) -> Path:
    bin_dir = tmp / "counting-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(PASSED_ARG_SCRIPT)
    stub.chmod(0o755)
    return stub


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_uv_bin_hash_mismatch_is_refused_before_any_use(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    with pytest.raises(ReleaseGateError, match="uv-hash-mismatch"):
        run_replay(
            candidate, "candidate", tmp_path / "replay", stub, None, ("-q",),
            uv_sha256="f" * 64,
        )
    assert not (tmp_path / "replay" / "clean-room").exists()


def test_uv_bin_hash_pinned_run_passes(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay", stub, None, ("-q",),
        uv_sha256=_sha(stub),
    )
    assert report.verdict == "passed"


def test_missing_uv_sha256_for_candidate_is_refused(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    with pytest.raises(ReleaseGateError, match="uv-sha-required"):
        run_replay(candidate, "candidate", tmp_path / "replay", stub, None, ("-q",))


def test_acceptance_step_must_report_executed_test_count(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-counting", counting, None, ("-q",),
        uv_sha256=_sha(counting),
    )
    assert report.verdict == "passed"
    acceptance = next(step for step in report.steps if step.name == "acceptance-offline")
    assert acceptance.tests_passed == 1234

    no_summary = make_stub_uv(tmp_path)
    no_summary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        'if argv[:1] == ["sync"]:\n'
        "    sys.exit(0)\n"
        'if "pytest" in argv:\n'
        '    print("nothing was executed")\n'
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    no_summary.chmod(0o755)
    weak = run_replay(
        candidate, "candidate", tmp_path / "replay-weak", no_summary, None, ("-q",),
        uv_sha256=_sha(no_summary),
    )
    assert weak.verdict == "failed"


def test_min_passed_floor_enforced(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-floor", counting, None, ("-q",),
        uv_sha256=_sha(counting), min_passed=5000,
    )
    assert report.verdict == "failed"


def test_child_environment_strips_credentials_and_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {
        "EDITORIAL_DIRECTOR_API_KEY": "sk-live-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "GITHUB_TOKEN": "ghp-secret",
        "EDITORIAL_DIRECTOR_BASE_URL": "https://evil.example",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    environment = child_environment(tmp_path)
    for key in secrets:
        assert key not in environment
    allowed = {"PATH", "HOME", "TMPDIR", "TZ", "LANG", "LC_CTYPE"}
    assert set(environment) <= allowed | {
        "UV_PROJECT_ENVIRONMENT",
        "UV_CACHE_DIR",
        "UV_OFFLINE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
    }


def test_guard_limitations_are_recorded_honestly() -> None:
    assert "Python-socket-level" in GUARD_LIMITATIONS
    assert "OS-level sandbox" in GUARD_LIMITATIONS
    assert "marked paths" in GUARD_LIMITATIONS


def test_guard_limitations_ride_with_the_report(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-limitations", counting, None, ("-q",),
        uv_sha256=_sha(counting),
    )
    assert report.guard_limitations == GUARD_LIMITATIONS
    assert (tmp_path / "replay-limitations" / "replay-report.json").is_file()


def test_diagnostic_run_never_carries_bare_passed_verdict(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-diagnostic", counting, None,
        ("-q", "--ignore=tests/release"), uv_sha256=_sha(counting), diagnostic=True,
    )
    assert report.verdict == "diagnostic-passed"
    assert report.verdict_scope == "diagnostic"
    assert report.verdict != "passed"
    persisted = json.loads(
        (tmp_path / "replay-diagnostic" / "replay-report.json").read_text()
    )
    assert persisted["verdict"] == "diagnostic-passed"
    assert persisted["verdict_scope"] == "diagnostic"


def test_acceptance_run_carries_acceptance_scope(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-acceptance", counting, None, ("-q",),
        uv_sha256=_sha(counting),
    )
    assert report.verdict == "passed"
    assert report.verdict_scope == "acceptance"


def test_diagnostic_failure_is_failed_verdict(tmp_path: Path) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    failing = make_failing_uv(tmp_path)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-diag-fail", failing, None, ("-q",),
        uv_sha256=_sha(failing), diagnostic=True,
    )
    assert report.verdict == "failed"
    assert report.verdict_scope == "diagnostic"


@pytest.mark.parametrize("min_passed", [0, -1, -100])
def test_non_positive_min_passed_is_typed_refusal(
    tmp_path: Path, min_passed: int
) -> None:
    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    with pytest.raises(ReleaseGateError, match="min-passed-invalid"):
        run_replay(
            candidate, "candidate", tmp_path / "replay-minpassed", stub, None, ("-q",),
            uv_sha256=_sha(stub), min_passed=min_passed,
        )


def test_min_passed_floor_is_non_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overrides may raise the floor; they can never lower it below the
    non-overridable acceptance floor constant."""

    import services.release.replay as replay_module  # noqa: PLC0415

    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    counting = _counting_uv(tmp_path)
    monkeypatch.setattr(replay_module, "MIN_PASSED_FLOOR", 2000)
    report = run_replay(
        candidate, "candidate", tmp_path / "replay-floor-const", counting, None,
        ("-q",), uv_sha256=_sha(counting), min_passed=1,
    )
    assert report.verdict == "failed"


def test_cli_min_passed_zero_is_typed_refusal(tmp_path: Path, capsys) -> None:
    from services.release.replay import main as replay_main  # noqa: PLC0415

    _repo, candidate, _git_sha = build_test_candidate(tmp_path)
    stub = make_stub_uv(tmp_path)
    rc = replay_main(
        [
            "--candidate", str(candidate),
            "--out", str(tmp_path / "replay-cli-zero"),
            "--uv-bin", str(stub),
            "--uv-sha256", _sha(stub),
            "--min-passed", "0",
        ]
    )
    assert rc == 2
    assert "min-passed-invalid" in capsys.readouterr().err
