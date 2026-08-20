"""Todo 45 CLI acceptance: the exact H1 ``checkpoint show|record|export`` surface.

``show`` rehashes the bundle and displays the target hashes (the TTY
receipt); ``record`` creates FIXTURE-MARKED records on the QA seam and
refuses to mint real approvals without a controlling TTY; ``export``
rehashes every target, fails on drift, and rejects fixture records
presented as real. The CLI package carries no UI dependency.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from services.cli.bundle import load_bundle
from tests.approvals.support import wait_with_master_drain

if TYPE_CHECKING:
    from tests.cli.conftest import CliRig

FORBIDDEN_UI_TOKENS = ("tkinter", "PyQt", "streamlit", "flask", "gradio", "webview", "pygame")


def run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, check=False, cwd=Path.cwd()
    )


def show(rig: CliRig, receipt: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        ["-m", "services.cli.checkpoint", "show", "--bundle", str(rig.bundle_file),
         "--receipt", str(receipt)]
    )


def record(rig: CliRig, receipt: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_cli(
        ["-m", "services.cli.checkpoint", "record", "--bundle", str(rig.bundle_file),
         "--display-receipt", str(receipt), "--purpose", "EDITORIAL_APPROVED",
         "--decision", "approve", "--out", str(out), *extra]
    )


def export(
    rig: CliRig, receipt: Path, op_record: Path, out: Path
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        ["-m", "services.cli.checkpoint", "export", "--bundle", str(rig.bundle_file),
         "--display-receipt", str(receipt), "--operation-record", str(op_record),
         "--out", str(out)]
    )


def test_00_checkpoint_help_surface_matches_the_plan() -> None:
    result = run_cli(["-m", "services.cli.checkpoint", "--help"])
    assert result.returncode == 0
    for token in ("show", "record", "export"):
        assert token in result.stdout
    show_help = run_cli(["-m", "services.cli.checkpoint", "show", "--help"])
    assert show_help.returncode == 0
    assert "--bundle" in show_help.stdout
    assert "--receipt" in show_help.stdout
    record_help = run_cli(["-m", "services.cli.checkpoint", "record", "--help"])
    assert record_help.returncode == 0
    for token in ("--bundle", "--display-receipt", "--purpose", "--decision", "--out"):
        assert token in record_help.stdout
    export_help = run_cli(["-m", "services.cli.checkpoint", "export", "--help"])
    assert export_help.returncode == 0
    for token in ("--bundle", "--display-receipt", "--operation-record", "--out"):
        assert token in export_help.stdout


def test_10_show_displays_and_seals_the_target_hashes(cli_rig: CliRig) -> None:
    receipt = cli_rig.root / "display.json"
    result = show(cli_rig, receipt)
    assert result.returncode == 0, result.stderr
    bundle = load_bundle(cli_rig.bundle_file)
    assert bundle.current.plan_sha256 in result.stdout
    assert bundle.current.preview_sha256 in result.stdout
    assert "EDITORIAL_APPROVED" in result.stdout
    sealed = json.loads(receipt.read_bytes())
    assert sealed["purpose"] == "EDITORIAL_APPROVED"
    assert sealed["targets"]["plan_sha256"] == bundle.current.plan_sha256
    assert sealed["targets"]["plan_version"] == "v1"


def test_20_record_fixture_path_creates_fixture_marked_record(cli_rig: CliRig) -> None:
    receipt = cli_rig.root / "display.json"
    assert show(cli_rig, receipt).returncode == 0
    op_record = cli_rig.root / "op-fixture.jsonl"
    result = record(cli_rig, receipt, op_record, "--fixture")
    assert result.returncode == 0, result.stderr
    assert "FIXTURE-MARKED" in result.stdout
    line = json.loads(op_record.read_bytes().splitlines()[0])
    assert line["fixture_only"] is True
    assert line["runner_class"] == "automation"
    assert line["decision"] == "approve"
    assert line["purpose"] == "editorial"


def test_30_record_without_a_tty_refuses_to_mint_a_real_approval(cli_rig: CliRig) -> None:
    receipt = cli_rig.root / "display.json"
    assert show(cli_rig, receipt).returncode == 0
    assert not sys.stdin.isatty(), "QA must run without a controlling TTY on stdin"
    result = record(cli_rig, receipt, cli_rig.root / "op-real.jsonl")
    assert result.returncode == 1
    assert "not-a-tty" in result.stderr
    assert not (cli_rig.root / "op-real.jsonl").exists()


def test_40_fixture_record_presented_as_real_is_rejected_by_export(cli_rig: CliRig) -> None:
    receipt = cli_rig.root / "display.json"
    assert show(cli_rig, receipt).returncode == 0
    op_record = cli_rig.root / "op-fixture.jsonl"
    assert record(cli_rig, receipt, op_record, "--fixture").returncode == 0
    result = export(cli_rig, receipt, op_record, cli_rig.root / "checkpoint.json")
    assert result.returncode == 1
    assert "fixture" in result.stderr
    assert not (cli_rig.root / "checkpoint.json").exists()


def test_50_export_rehashes_targets_and_fails_on_drift(cli_rig: CliRig) -> None:
    receipt = cli_rig.root / "display.json"
    assert show(cli_rig, receipt).returncode == 0
    op_record = cli_rig.root / "op-drift.jsonl"
    assert record(cli_rig, receipt, op_record, "--fixture").returncode == 0
    preview = (
        cli_rig.bundle_file.parent / "preview-v1" / "preview.mp4"
    )
    original = preview.read_bytes()
    try:
        preview.write_bytes(original + b"\x00drift")
        drifted_show = show(cli_rig, cli_rig.root / "display-drift.json")
        assert drifted_show.returncode == 1
        assert "target_drift" in drifted_show.stderr
        drifted_export = export(
            cli_rig, receipt, op_record, cli_rig.root / "checkpoint-drift.json"
        )
        assert drifted_export.returncode == 1
        assert not (cli_rig.root / "checkpoint-drift.json").exists()
    finally:
        preview.write_bytes(original)


def test_60_cli_package_has_no_ui_dependency() -> None:
    offenders = [
        f"{path.name}:{token}"
        for path in sorted(Path("services/cli").glob("*.py"))
        for token in FORBIDDEN_UI_TOKENS
        if token.lower() in path.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"UI dependencies are forbidden in the H1 CLI: {offenders}"


def test_70_real_tty_record_and_fixture_lineage_checkpoint_refusal(cli_rig: CliRig) -> None:
    """A REAL TTY record on a FIXTURE lineage still cannot produce a checkpoint.

    The record child acquires the pty as its controlling terminal (a real
    terminal login in miniature — the hardened ingress refuses anything
    less), yet the fixture lineage still blocks the checkpoint export.
    """

    receipt = cli_rig.root / "display.json"
    assert show(cli_rig, receipt).returncode == 0
    op_record = cli_rig.root / "op-tty.jsonl"
    master, slave = pty.openpty()
    tty_path = os.ttyname(slave)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.approvals.ctty_child",
                tty_path,
                "--stdin-runpy",
                str(cli_rig.root / "ctty-result.json"),
                "services.cli.checkpoint",
                "record",
                "--bundle",
                str(cli_rig.bundle_file),
                "--display-receipt",
                str(receipt),
                "--purpose",
                "EDITORIAL_APPROVED",
                "--decision",
                "approve",
                "--out",
                str(op_record),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=Path.cwd(),
        )
        os.write(master, b"confirm\n")
        wait_with_master_drain(master, process, 60)
        stdout, stderr = process.communicate(timeout=60)
    finally:
        os.close(master)
        os.close(slave)
    assert process.returncode == 0, stderr
    assert "real operator" in stdout
    line = json.loads(op_record.read_bytes().splitlines()[0])
    assert line["fixture_only"] is False
    assert line["tty"]
    result = export(cli_rig, receipt, op_record, cli_rig.root / "checkpoint-tty.json")
    assert result.returncode == 1
    assert "fixture_lineage_rejected" in result.stderr
    assert not (cli_rig.root / "checkpoint-tty.json").exists()
