from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, assert_never

import pytest

from services.evidence.models import CommandSpec, ExitCodeAssertion, StdoutContainsAssertion
from services.evidence.runner import execute_command
from services.evidence.verify import EvidenceVerificationError, verify_bundle

VALID_FIXTURE = Path("tests/fixtures/evidence/valid")


def _valid_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    specification = CommandSpec(
        attempt_id="attempt-valid",
        argv=(sys.executable, "-c", "print('verified')"),
        cwd=tmp_path,
        inputs=(),
        outputs=(),
        assertions=(
            ExitCodeAssertion(expected=0, passed=True),
            StdoutContainsAssertion(expected="verified", passed=True),
        ),
    )
    execute_command(bundle, specification)
    return bundle


def test_retained_head_chain_and_raw_assertions_verify(tmp_path: Path) -> None:
    report = verify_bundle(_valid_bundle(tmp_path), recompute=True)

    assert report.chain_valid is True
    assert report.recomputed_passed is True
    assert report.incomplete_attempt_ids == ()


def test_checked_in_valid_fixture_recomputes_without_authored_passes() -> None:
    report = verify_bundle(VALID_FIXTURE, recompute=True)

    assert report.recomputed_passed is True
    assert all(result.authored_passed is False for result in report.assertions)


@pytest.mark.parametrize("mutation", ["entry", "reorder", "interior-delete"])
def test_retained_chain_tampering_is_rejected(
    tmp_path: Path,
    mutation: Literal["entry", "reorder", "interior-delete"],
) -> None:
    bundle = _valid_bundle(tmp_path)
    ledger = bundle / "ledger.jsonl"
    rows = ledger.read_text().splitlines()
    match mutation:
        case "entry":
            event = json.loads(rows[0])
            event["cwd"] = "/tampered"
            rows[0] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        case "reorder":
            rows.reverse()
        case "interior-delete":
            rows.pop(0)
        case unreachable:
            assert_never(unreachable)
    ledger.write_text("\n".join(rows) + "\n")

    with pytest.raises(EvidenceVerificationError):
        verify_bundle(bundle, recompute=True)


def test_authored_passed_is_ignored_when_raw_output_disagrees(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    specification = CommandSpec(
        attempt_id="attempt-forged",
        argv=(sys.executable, "-c", "print('actual')"),
        cwd=tmp_path,
        inputs=(),
        outputs=(),
        assertions=(
            StdoutContainsAssertion(expected="invented", passed=True),
        ),
    )
    execute_command(bundle, specification)

    report = verify_bundle(bundle, recompute=True)

    assert report.chain_valid is True
    assert report.recomputed_passed is False
    assert report.assertions[0].authored_passed is True
    assert report.assertions[0].recomputed_passed is False


def test_corrupt_retained_line_is_rejected(tmp_path: Path) -> None:
    bundle = _valid_bundle(tmp_path)
    ledger = bundle / "ledger.jsonl"
    ledger.write_bytes(b"{corrupt}\n" + ledger.read_bytes().splitlines(keepends=True)[1])

    with pytest.raises(EvidenceVerificationError, match="ledger row 1"):
        verify_bundle(bundle, recompute=True)


def test_unretained_partial_tail_is_reported_and_ignored(tmp_path: Path) -> None:
    bundle = _valid_bundle(tmp_path)
    with (bundle / "ledger.jsonl").open("ab") as stream:
        stream.write(b'{"event_type":"completion"')

    report = verify_bundle(bundle, recompute=True)

    assert report.recomputed_passed is True
    assert report.partial_tail_ignored is True
