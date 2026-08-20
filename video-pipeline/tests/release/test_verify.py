"""Verify gates: stale hash, forged report, failed Gate, chat/timeline
dependency, misleading KPI claim, missing H1 binding, writable candidate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.release.verify import verify_candidate
from services.release.verify_candidate import main as verify_main
from tests.release.support import (
    build_test_candidate,
    expect_gate_error,
    gate_result_bytes,
    make_writable,
)

HEX = "0" * 63 + "1"


def f1_argv(candidate: Path, sha: str) -> list[str]:
    return [
        "--candidate",
        str(candidate),
        "--expected-git-sha",
        sha,
        "--schema",
        "manifest-v1",
        "--require-total-h1-binding",
        "--recompute",
    ]


def rewrite(candidate: Path, relative: str, payload: bytes) -> None:
    target = candidate / relative
    make_writable(target)
    target.write_bytes(payload)


def remove(candidate: Path, relative: str) -> None:
    target = candidate / relative
    make_writable(target.parent)
    target.unlink()


def remanifest(candidate: Path) -> None:
    """Attacker helper: rewrite manifest.json consistently, restore 0444/0555."""

    from services.release.build_candidate import (  # noqa: PLC0415
        chmod_readonly,
    )
    from services.release.manifest import build_manifest, manifest_bytes  # noqa: PLC0415

    for directory, _, files in os.walk(candidate):
        Path(directory).chmod(0o755)
        for name in files:
            (Path(directory) / name).chmod(0o644)
    (candidate / "manifest.json").write_bytes(manifest_bytes(build_manifest(candidate)))
    chmod_readonly(candidate)


def verify_ok(candidate: Path, sha: str, *, recompute: bool = False) -> object:
    return verify_candidate(
        candidate,
        expected_git_sha=sha,
        require_h1_binding=True,
        recompute=recompute,
        require_readonly=False,
    )


def test_10_f1_flags_happy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    assert verify_main(f1_argv(candidate, sha)) == 0
    assert '"candidate_id"' in capsys.readouterr().out


def test_11_bad_schema_value_refused(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    argv = f1_argv(candidate, sha)
    argv[argv.index("--schema") + 1] = "manifest-v2"
    assert verify_main(argv) == 2


def test_20_tampered_byte_is_stale_hash(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    target = candidate / "source/services/core.py"
    make_writable(target)
    target.write_text("VALUE = 3\n")
    target.chmod(0o444)
    assert verify_main(f1_argv(candidate, sha)) == 2


def test_21_wrong_expected_sha_is_stale_hash(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    assert verify_main(f1_argv(candidate, "f" * 40)) == 2


def forged_gate_payload() -> bytes:
    return json.dumps(
        {
            "schema_version": "gate-result-v1",
            "record_type": "gate_result",
            "gate_id": "phase-2",
            "gate_version": "v1",
            "policy_sha256": HEX,
            "passed": True,
            "evidence_bundles": [{"bundle_sha256": HEX, "raw_evidence_sha256s": [HEX]}],
            "criteria_results": [
                {"criterion_id": "c-1", "passed": False, "raw_evidence_sha256s": [HEX]}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_30_forged_gate_report_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    rewrite(candidate, "inputs/gates/phase-2/gate-result.json", forged_gate_payload())
    remanifest(candidate)
    expect_gate_error(
        "forged_report",
        lambda: verify_ok(candidate, sha, recompute=True),
    )


def test_31_forged_gate_id_swap_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    rewrite(candidate, "inputs/gates/phase-0a/gate-result.json", gate_result_bytes("phase-0b"))
    remanifest(candidate)
    expect_gate_error(
        "forged_report",
        lambda: verify_ok(candidate, sha, recompute=True),
    )


def test_32_failed_gate_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    rewrite(
        candidate,
        "inputs/gates/phase-3/gate-result.json",
        gate_result_bytes("phase-3", passed=False),
    )
    remanifest(candidate)
    expect_gate_error(
        "failed_gate",
        lambda: verify_ok(candidate, sha, recompute=False),
    )


def test_33_missing_gate_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    remove(candidate, "inputs/gates/phase-0c/gate-result.json")
    assert verify_main(f1_argv(candidate, sha)) == 2
    (candidate / "inputs/gates/phase-0c").chmod(0o555)


def test_40_chat_ledger_dependency_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    make_writable(candidate / "inputs")
    (candidate / "inputs" / "conversation.jsonl").write_bytes(
        b'{"role":"user","content":"cut here"}\n'
    )
    (candidate / "inputs").chmod(0o555)
    expect_gate_error(
        "chat_or_timeline_dependency",
        lambda: verify_ok(candidate, sha, recompute=False),
    )


def test_41_open_timeline_dependency_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    make_writable(candidate / "inputs")
    (candidate / "inputs" / "session.drp").write_bytes(b"project")
    (candidate / "inputs").chmod(0o555)
    expect_gate_error(
        "chat_or_timeline_dependency",
        lambda: verify_ok(candidate, sha, recompute=False),
    )


def test_50_misleading_kpi_claim_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    make_writable(candidate / "inputs")
    kpi_file = candidate / "inputs" / "kpi.json"
    kpi_file.write_bytes(b'{"active_human_time_median_ms": 1800000}')
    (candidate / "inputs").chmod(0o555)
    expect_gate_error(
        "misleading_kpi_claim",
        lambda: verify_ok(candidate, sha, recompute=False),
    )


def test_60_missing_h1_binding_detected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    remove(candidate, "inputs/h1/binding.json")
    expect_gate_error(
        "missing_h1_binding",
        lambda: verify_ok(candidate, sha, recompute=False),
    )


def test_61_fixture_only_h1_rejected(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    checkpoint = candidate / "inputs/h1/checkpoint-result.json"
    payload = checkpoint.read_bytes().replace(b'"fixture_only":false', b'"fixture_only":true')
    rewrite(candidate, "inputs/h1/checkpoint-result.json", payload)
    remanifest(candidate)
    expect_gate_error(
        "stale_hash",
        lambda: verify_ok(candidate, sha, recompute=True),
    )


def test_70_writable_candidate_fails_readonly_requirement(tmp_path: Path) -> None:
    _repo, candidate, sha = build_test_candidate(tmp_path)
    make_writable(candidate / "identity.json")
    try:
        verification = verify_candidate(
            candidate,
            expected_git_sha=sha,
            require_h1_binding=True,
            recompute=True,
            require_readonly=False,
        )
        assert verification.modes.all_readonly is False
        def require_readonly() -> object:
            return verify_candidate(
                candidate,
                expected_git_sha=sha,
                require_h1_binding=True,
                recompute=True,
                require_readonly=True,
            )

        expect_gate_error("writable_candidate", require_readonly)
    finally:
        (candidate / "identity.json").chmod(0o444)
