"""Todo 65: the six-file Global Review Report v1 set and FINAL_APPROVED record.

The FINAL_APPROVED checkpoint purpose accepts ONLY a complete Global Review
Report v1 set — exactly six fixed-name files, all APPROVE, one full SHA, one
candidate — recorded from a real local TTY (or the explicit ``--fixture``
seam). Every tampered dimension (missing/duplicate/stale/reject/mixed-SHA/
mixed-candidate/revoked/non-TTY) is a typed refusal, and the raw review-work /
debugging converter refuses malformed raw responses, including an omitted
STOP instruction.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import time
from pathlib import Path

import pytest

from services.approvals.global_review import load_report_set, load_revocations
from services.approvals.global_review_models import (
    REPORT_FILE_NAMES,
    GlobalReviewReport,
)

FULL_SHA = "1f" * 20
CANDIDATE = "cand-fixture-01"
LANES = (
    "goal-constraints",
    "code-quality",
    "security",
    "hands-on-qa",
    "context-mining",
    "debugging",
)


def run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, check=False, cwd=Path.cwd()
    )


def report(lane: str, **overrides: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "global-review-v1",
        "lane": lane,
        "full_sha": FULL_SHA,
        "candidate_id": CANDIDATE,
        "verdict": "APPROVE",
        "raw_response_sha256": "b" * 64,
        "artifact_hashes": {},
        "findings": ("no blocking issues",),
    }
    payload.update(overrides)
    return payload


def write_set(root: Path, *, lanes: tuple[str, ...] = LANES, **overrides: str) -> Path:
    directory = root / "report-set"
    directory.mkdir(parents=True)
    for lane in lanes:
        payload = report(lane, **overrides)
        (directory / f"{lane}.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def record(
    report_dir: Path, out: Path, *extra: str, decision: str = "approve"
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        [
            "-m", "services.cli.checkpoint", "record",
            "--purpose", "FINAL_APPROVED",
            "--work-id", "work-fixture-01",
            "--git-sha", FULL_SHA,
            "--candidate-id", CANDIDATE,
            "--report-dir", str(report_dir),
            "--decision", decision,
            "--out", str(out),
            *extra,
        ]
    )


def test_10_happy_report_set_validates_and_hashes(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    loaded = load_report_set(directory, full_sha=FULL_SHA, candidate_id=CANDIDATE)
    assert tuple(report.lane for report in loaded.reports) == LANES
    assert len(loaded.report_set_sha256) == 64
    assert all(report.verdict == "APPROVE" for report in loaded.reports)


def test_12_happy_fixture_record_seam_records_final_approval(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    out = tmp_path / "op-final.jsonl"
    result = record(directory, out, "--fixture")
    assert result.returncode == 0, result.stderr
    assert "FIXTURE-MARKED" in result.stdout
    line = json.loads(out.read_bytes().splitlines()[0])
    assert line["purpose"] == "final"
    assert line["fixture_only"] is True
    binding = line["final_binding"]
    assert binding["full_sha"] == FULL_SHA
    assert binding["candidate_id"] == CANDIDATE
    assert binding["work_id"] == "work-fixture-01"
    assert binding["report_set_sha256"] == load_report_set(
        directory, full_sha=FULL_SHA, candidate_id=CANDIDATE
    ).report_set_sha256


def test_14_final_approved_records_with_a_real_local_tty(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    out = tmp_path / "op-final-tty.jsonl"
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "services.cli.checkpoint", "record",
                "--purpose", "FINAL_APPROVED",
                "--work-id", "work-fixture-01",
                "--git-sha", FULL_SHA,
                "--candidate-id", CANDIDATE,
                "--report-dir", str(directory),
                "--decision", "approve",
                "--out", str(out),
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        time.sleep(1.0)
        os.write(master, b"confirm\n")
        stdout, stderr = process.communicate(timeout=60)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
    assert process.returncode == 0, stderr
    assert "real operator" in stdout
    line = json.loads(out.read_bytes().splitlines()[0])
    assert line["fixture_only"] is False
    assert line["uid"] == os.getuid()
    assert line["tty"]
    assert line["final_binding"]["candidate_id"] == CANDIDATE


def test_20_missing_report_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path, lanes=LANES[:5])
    with pytest.raises(ValueError, match="missing-report"):
        load_report_set(directory, full_sha=FULL_SHA, candidate_id=CANDIDATE)
    result = record(directory, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "missing-report" in result.stderr
    assert not (tmp_path / "op.jsonl").exists()


def test_22_extra_and_duplicate_reports_are_typed(tmp_path: Path) -> None:
    extra = write_set(tmp_path)
    (extra / "notes.json").write_text("{}", encoding="utf-8")
    result = record(extra, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "extra-report" in result.stderr

    duplicate = write_set(tmp_path / "dup")
    payload = report("security")
    (duplicate / "code-quality.json").write_text(json.dumps(payload), encoding="utf-8")
    result = record(duplicate, tmp_path / "op2.jsonl", "--fixture")
    assert result.returncode == 1
    assert "lane-name-mismatch" in result.stderr or "duplicate-lane" in result.stderr


def test_24_stale_report_sha_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    result = record(directory, tmp_path / "op.jsonl", "--fixture", "--git-sha", "ee" * 20)
    assert result.returncode == 1
    assert "stale-report" in result.stderr
    assert not (tmp_path / "op.jsonl").exists()


def test_26_reject_verdict_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path, verdict="REJECT")
    result = record(directory, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "verdict-not-approved" in result.stderr


def test_28_mixed_sha_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    payload = report("security", full_sha="9d" * 20)
    (directory / "security.json").write_text(json.dumps(payload), encoding="utf-8")
    result = record(directory, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "mixed-sha" in result.stderr


def test_30_mixed_candidate_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    payload = report("debugging", candidate_id="cand-other-02")
    (directory / "debugging.json").write_text(json.dumps(payload), encoding="utf-8")
    result = record(directory, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "mixed-candidate" in result.stderr


def test_32_non_tty_record_refused(tmp_path: Path) -> None:
    assert not sys.stdin.isatty(), "QA must run without a controlling TTY on stdin"
    directory = write_set(tmp_path)
    result = record(directory, tmp_path / "op-real.jsonl")
    assert result.returncode == 1
    assert "not-a-tty" in result.stderr
    assert not (tmp_path / "op-real.jsonl").exists()


def test_34_revoked_candidate_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    revocations = tmp_path / "revocations.json"
    revocations.write_text(
        json.dumps(
            {
                "schema_version": "candidate-revocations-v1",
                "revoked": [
                    {"candidate_id": CANDIDATE, "reason": "superseded rebuild"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert CANDIDATE in load_revocations(revocations)
    result = record(
        directory, tmp_path / "op.jsonl", "--fixture", "--revocations", str(revocations)
    )
    assert result.returncode == 1
    assert "candidate-revoked" in result.stderr
    assert not (tmp_path / "op.jsonl").exists()


def test_36_malformed_report_is_typed(tmp_path: Path) -> None:
    directory = write_set(tmp_path)
    (directory / "security.json").write_text("{ not json", encoding="utf-8")
    result = record(directory, tmp_path / "op.jsonl", "--fixture")
    assert result.returncode == 1
    assert "malformed-report" in result.stderr


def test_40_converter_markdown_happy(tmp_path: Path) -> None:
    raw = tmp_path / "raw-security.md"
    raw.write_text(
        "lane: security\n"
        f"full_sha: {FULL_SHA}\n"
        f"candidate_id: {CANDIDATE}\n"
        "verdict: PASS\n"
        "finding: no blocking issues in the reviewed range\n"
        "artifact: report-set=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "STOP: no blockers; approval may proceed\n",
        encoding="utf-8",
    )
    out = tmp_path / "security.json"
    result = run_cli(
        [
            "-m", "services.cli", "convert-review",
            "--raw", str(raw),
            "--lane", "security",
            "--full-sha", FULL_SHA,
            "--candidate-id", CANDIDATE,
            "--out", str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    converted = GlobalReviewReport.model_validate_json(out.read_bytes())
    assert converted.verdict == "APPROVE"
    assert converted.lane == "security"
    assert converted.findings == ("no blocking issues in the reviewed range",)


def test_42_converter_json_happy(tmp_path: Path) -> None:
    raw = tmp_path / "raw-debugging.json"
    raw.write_text(
        json.dumps(
            {
                "lane": "debugging",
                "full_sha": FULL_SHA,
                "candidate_id": CANDIDATE,
                "verdict": "APPROVE",
                "stop": "audit-only: every fault route verified, candidate unchanged",
                "findings": ["restart verified", "stale-state verified"],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "debugging.json"
    result = run_cli(
        [
            "-m", "services.cli", "convert-review",
            "--raw", str(raw),
            "--lane", "debugging",
            "--full-sha", FULL_SHA,
            "--candidate-id", CANDIDATE,
            "--out", str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    converted = GlobalReviewReport.model_validate_json(out.read_bytes())
    assert converted.verdict == "APPROVE"
    assert converted.findings == ("restart verified", "stale-state verified")


def test_44_converter_omitted_stop_instruction_is_typed(tmp_path: Path) -> None:
    raw = tmp_path / "raw-nostop.md"
    raw.write_text(
        f"lane: security\nfull_sha: {FULL_SHA}\ncandidate_id: {CANDIDATE}\nverdict: PASS\n",
        encoding="utf-8",
    )
    result = run_cli(
        [
            "-m", "services.cli", "convert-review",
            "--raw", str(raw), "--lane", "security",
            "--full-sha", FULL_SHA, "--candidate-id", CANDIDATE,
            "--out", str(tmp_path / "out.json"),
        ]
    )
    assert result.returncode == 1
    assert "stop-instruction-omitted" in result.stderr
    assert not (tmp_path / "out.json").exists()


def test_46_converter_binding_mismatches_are_typed(tmp_path: Path) -> None:
    raw = tmp_path / "raw-mismatch.md"
    out = str(tmp_path / "o.json")
    base = ["-m", "services.cli", "convert-review", "--raw", str(raw), "--out", out]
    tail = ["--lane", "security", "--full-sha", FULL_SHA, "--candidate-id", CANDIDATE]

    def raw_text(lane: str, sha: str, candidate: str) -> str:
        return (
            f"lane: {lane}\nfull_sha: {sha}\ncandidate_id: {candidate}\n"
            "verdict: PASS\nSTOP: ok\n"
        )

    raw.write_text(raw_text("security", "9d" * 20, CANDIDATE), encoding="utf-8")
    wrong_sha = run_cli([*base, *tail])
    assert wrong_sha.returncode == 1
    assert "sha-mismatch" in wrong_sha.stderr
    raw.write_text(raw_text("security", FULL_SHA, "cand-other-02"), encoding="utf-8")
    wrong_candidate = run_cli([*base, *tail])
    assert wrong_candidate.returncode == 1
    assert "candidate-mismatch" in wrong_candidate.stderr
    raw.write_text(raw_text("debugging", FULL_SHA, CANDIDATE), encoding="utf-8")
    wrong_lane = run_cli([*base, *tail])
    assert wrong_lane.returncode == 1
    assert "lane-mismatch" in wrong_lane.stderr


def test_48_converter_blockers_present_is_typed(tmp_path: Path) -> None:
    raw = tmp_path / "raw-blockers.md"
    raw.write_text(
        f"lane: security\nfull_sha: {FULL_SHA}\ncandidate_id: {CANDIDATE}\n"
        "verdict: PASS\nblocker: secret leaked in logs\nSTOP: fix before approval\n",
        encoding="utf-8",
    )
    result = run_cli(
        [
            "-m", "services.cli", "convert-review",
            "--raw", str(raw), "--lane", "security",
            "--full-sha", FULL_SHA, "--candidate-id", CANDIDATE,
            "--out", str(tmp_path / "out.json"),
        ]
    )
    assert result.returncode == 1
    assert "blockers-present" in result.stderr


def test_49_converter_malformed_raw_is_typed(tmp_path: Path) -> None:
    raw = tmp_path / "raw-malformed.md"
    raw.write_text("this raw response has no contract lines at all\n", encoding="utf-8")
    result = run_cli(
        [
            "-m", "services.cli", "convert-review",
            "--raw", str(raw), "--lane", "security",
            "--full-sha", FULL_SHA, "--candidate-id", CANDIDATE,
            "--out", str(tmp_path / "out.json"),
        ]
    )
    assert result.returncode == 1
    assert "malformed-raw" in result.stderr


def test_50_record_requires_final_arguments(tmp_path: Path) -> None:
    result = run_cli(
        [
            "-m", "services.cli.checkpoint", "record",
            "--purpose", "FINAL_APPROVED",
            "--work-id", "work-fixture-01",
            "--decision", "approve",
            "--out", str(tmp_path / "op.jsonl"),
        ]
    )
    assert result.returncode == 1
    assert "missing-final-argument" in result.stderr
    assert not (tmp_path / "op.jsonl").exists()


def test_52_fixed_report_file_names_are_the_six_lanes() -> None:
    assert REPORT_FILE_NAMES == (
        "goal-constraints.json",
        "code-quality.json",
        "security.json",
        "hands-on-qa.json",
        "context-mining.json",
        "debugging.json",
    )
