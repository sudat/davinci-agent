"""Shared builders for release-candidate tests: synthetic Git repo, attempt
directory with Gate/H1/task evidence, a fully built candidate, and a stub uv."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from services.foundation_io import canonical_model_bytes
from services.gates.models import CriterionResult, EvidenceBundleRef, GateResult
from services.release.errors import ReleaseGateError
from services.release.models import GATE_DIRS

HEX64 = "0" * 63 + "1"


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
    )


def make_git_repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "workspace"
    (repo / "video-pipeline").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    source = repo / "video-pipeline"
    (source / "pyproject.toml").write_text(
        "[project]\nname = 'video-pipeline'\nversion = '0.1.0'\n"
    )
    services_dir = source / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").write_text("")
    (services_dir / "core.py").write_text("VALUE = 1\n")
    tests_dir = source / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text("def test_value():\n    assert 1 == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, sha


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_more(repo: Path) -> str:
    source = repo / "video-pipeline"
    (source / "services" / "core.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    return _head(repo)


def gate_result_bytes(gate_id: str, *, passed: bool = True) -> bytes:
    result = GateResult(
        schema_version="gate-result-v1",
        gate_id=gate_id,
        gate_version="v1",
        policy_sha256=HEX64,
        passed=passed,
        evidence_bundles=(
            EvidenceBundleRef(bundle_sha256=HEX64, raw_evidence_sha256s=(HEX64,)),
        ),
        criteria_results=(
            CriterionResult(criterion_id="c-1", passed=passed, raw_evidence_sha256s=(HEX64,)),
        ),
    )
    return canonical_model_bytes(result)


def h1_checkpoint_bytes() -> bytes:
    payload = {
        "schema_version": "operator-checkpoint-v1",
        "record_type": "operator_checkpoint",
        "purpose": "EDITORIAL_APPROVED",
        "fixture_only": False,
        "episode_id": "real-01",
        "edit_source_world_sha256": HEX64,
        "final_plan_sha256": HEX64,
        "final_ir_sha256": HEX64,
        "final_preview_sha256": HEX64,
        "actor_id": "local-operator",
    }
    return _canonical(payload)


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def expect_gate_error(code: str, action: Callable[[], object]) -> ReleaseGateError:
    """Run ``action`` and assert a typed release-prevention code."""

    with pytest.raises(ReleaseGateError) as excinfo:
        action()
    assert excinfo.value.code == code
    return excinfo.value


def make_attempt_dir(tmp: Path) -> Path:
    attempt = tmp / "attempt"
    for gate_dir, gate_id in GATE_DIRS.items():
        target = attempt / gate_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "gate-result.json").write_bytes(gate_result_bytes(gate_id))
    h1 = attempt / "h1"
    h1.mkdir(exist_ok=True)
    (h1 / "checkpoint-result.json").write_bytes(h1_checkpoint_bytes())
    for number in range(1, 67):
        (attempt / f"task-{number}-foundation-video-pipeline.json").write_bytes(
            _canonical({"todo": number, "all_observed": True})
        )
    return attempt


def make_ledgers(tmp: Path) -> tuple[Path, Path]:
    start_work = tmp / "ledger.jsonl"
    start_work.write_bytes(
        b'{"event":"work-initialized","todo":"todo-1"}\n'
        b'{"event":"task-dispatched","task":"todo-66"}\n'
        b'{"event":"task-dispatched","task":"todo-67"}\n'
    )
    execution = tmp / "execution-ledger.jsonl"
    execution.write_bytes(b'{"event_type":"freeze-intent","sequence":1}\n')
    return start_work, execution


def build_test_candidate(tmp: Path) -> tuple[Path, Path, str]:
    """Full happy build; returns (repo, candidate_path, git_sha)."""

    from services.release.build_candidate import main as build_main  # noqa: PLC0415

    repo, sha = make_git_repo(tmp)
    attempt = make_attempt_dir(tmp)
    start_work, execution = make_ledgers(tmp)
    candidate = tmp / "release-candidates" / sha
    receipt = tmp / "receipt.json"
    exit_code = build_main(
        [
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
            str(attempt),
            "--receipt",
            str(receipt),
            "--phase",
            "all",
        ]
    )
    assert exit_code == 0
    return repo, candidate, sha


def make_plan(tmp: Path) -> Path:
    plan = tmp / "plan.md"
    plan.write_text("# plan\n- [x] 66\n- [ ] 67\n")
    return plan


def todo67_evidence_bytes() -> bytes:
    from services.qa.run_todo import CaseEvidence, TodoEvidence  # noqa: PLC0415

    evidence = TodoEvidence(
        todo=67,
        cases=(
            CaseEvidence(
                case_id="happy",
                kind="happy",
                argv=("pytest", "tests/release", "-q"),
                exit_code=0,
                stdout="1 passed",
                stderr="",
                artifacts=(),
                observed=True,
            ),
        ),
        all_observed=True,
    )
    return canonical_model_bytes(evidence)


def make_writable(path: Path) -> None:
    mode = stat.S_IMODE(path.lstat().st_mode)
    path.chmod(mode | 0o200)


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
