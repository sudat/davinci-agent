from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.evidence.models import CommandSpec, ExitCodeAssertion, StdoutContainsAssertion
from services.evidence.runner import execute_command


def test_ledger_fsync_occurs_before_subprocess_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsynced_descriptors: list[int] = []
    system_fsync = os.fsync
    system_run = subprocess.run

    def record_fsync(descriptor: int) -> None:
        fsynced_descriptors.append(descriptor)
        system_fsync(descriptor)

    def require_prior_fsync(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        assert fsynced_descriptors
        return system_run(argv, cwd=cwd, check=check, capture_output=capture_output)

    monkeypatch.setattr("services.evidence.ledger.os.fsync", record_fsync)
    monkeypatch.setattr("services.evidence.runner.subprocess.run", require_prior_fsync)
    specification = CommandSpec(
        attempt_id="attempt-fsync",
        argv=(sys.executable, "-c", "print('ran')"),
        cwd=tmp_path,
        inputs=(),
        outputs=(),
        assertions=(ExitCodeAssertion(expected=0, passed=True),),
    )

    result = execute_command(tmp_path / "bundle", specification)

    assert result.exit_code == 0


def test_attempt_is_fsynced_before_product_command_launches(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    marker = tmp_path / "observed.txt"
    script = (
        "import json, pathlib, sys; "
        "root=pathlib.Path(sys.argv[1]); "
        "rows=root.joinpath('ledger.jsonl').read_text().splitlines(); "
        "head=json.loads(root.joinpath('head.json').read_text()); "
        "event=json.loads(rows[-1]); "
        "pathlib.Path(sys.argv[2]).write_text(str(event['event_type'] == 'attempt' "
        "and head['event_hash'] == event['event_hash']))"
    )
    specification = CommandSpec(
        attempt_id="attempt-prelaunch",
        argv=(sys.executable, "-c", script, str(bundle), str(marker)),
        cwd=tmp_path,
        inputs=(),
        outputs=(marker,),
        assertions=(ExitCodeAssertion(expected=0, passed=True),),
    )

    result = execute_command(bundle, specification)

    assert result.exit_code == 0
    assert marker.read_text() == "True"
    rows = [json.loads(line) for line in (bundle / "ledger.jsonl").read_text().splitlines()]
    assert [row["event_type"] for row in rows] == ["attempt", "completion"]
    assert rows[1]["previous_event_hash"] == rows[0]["event_hash"]
    assert rows[1]["outputs"][0]["sha256"]


def test_completion_retains_raw_streams_and_file_hashes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    source = tmp_path / "source.txt"
    output = tmp_path / "output.txt"
    source.write_text("input")
    script = (
        "import pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text('output'); "
        "print('gate-ready'); print('diagnostic', file=sys.stderr)"
    )
    specification = CommandSpec(
        attempt_id="attempt-raw",
        argv=(sys.executable, "-c", script, str(output)),
        cwd=tmp_path,
        inputs=(source,),
        outputs=(output,),
        assertions=(
            ExitCodeAssertion(expected=0, passed=True),
            StdoutContainsAssertion(expected="gate-ready", passed=True),
        ),
    )

    result = execute_command(bundle, specification)

    assert result.stdout == b"gate-ready\n"
    assert result.stderr == b"diagnostic\n"
    assert (bundle / "raw/attempt-raw.stdout").read_bytes() == result.stdout
    assert (bundle / "raw/attempt-raw.stderr").read_bytes() == result.stderr
    completion = json.loads((bundle / "ledger.jsonl").read_text().splitlines()[1])
    assert completion["inputs"][0]["path"] == str(source.resolve())
    assert completion["outputs"][0]["path"] == str(output.resolve())
