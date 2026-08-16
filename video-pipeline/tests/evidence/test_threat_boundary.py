from __future__ import annotations

import sys
from pathlib import Path

from services.evidence.models import CommandSpec, ExitCodeAssertion
from services.evidence.runner import execute_command
from services.evidence.verify import verify_bundle

EXPECTED_BOUNDARIES = (
    "unrecorded executions cannot be detected",
    "full ledger and retained-head replacement cannot be detected",
    "owner or root rewrites cannot be prevented",
)


def _replacement_bundle(tmp_path: Path, attempt_id: str) -> Path:
    bundle = tmp_path / attempt_id
    execute_command(
        bundle,
        CommandSpec(
            attempt_id=attempt_id,
            argv=(sys.executable, "-c", "print('replacement')"),
            cwd=tmp_path,
            inputs=(),
            outputs=(),
            assertions=(ExitCodeAssertion(expected=0, passed=True),),
        ),
    )
    return bundle


def test_unrecorded_execution_is_explicitly_outside_guarantee(tmp_path: Path) -> None:
    bundle = _replacement_bundle(tmp_path, "recorded")
    (tmp_path / "unrecorded-side-effect.txt").write_text("not in ledger")

    report = verify_bundle(bundle, recompute=True)

    assert report.outside_guarantee == EXPECTED_BOUNDARIES


def test_full_ledger_and_head_replacement_is_explicitly_outside_guarantee(
    tmp_path: Path,
) -> None:
    original = _replacement_bundle(tmp_path, "original")
    replacement = _replacement_bundle(tmp_path, "replacement")
    (original / "ledger.jsonl").write_bytes((replacement / "ledger.jsonl").read_bytes())
    (original / "head.json").write_bytes((replacement / "head.json").read_bytes())
    original_raw = original / "raw"
    replacement_raw = replacement / "raw"
    for path in replacement_raw.iterdir():
        (original_raw / path.name).write_bytes(path.read_bytes())

    report = verify_bundle(original, recompute=True)

    assert report.chain_valid is True
    assert report.outside_guarantee == EXPECTED_BOUNDARIES
