"""Regression (round-2 QA): the phase-2 fault CLI must follow the
cascade-tracked current policy version and turn GateP2Error into a typed
fault-error exit — never an unhandled traceback.

At the frozen chain state (phase-2 v4 + its cascade receipt) the CLI
previously crashed: POLICY was hard-pinned to phase-2-v1.json while the
receipt resolved to gate-cascade/receipts/phase-2-v4.json, so
GateP2Error(freeze-receipt-binding-drift) escaped as a traceback with
exit 1, violating the 0=detected / 2=missed-or-typed-error contract.

This module is workspace-anchored (reads the live attempt directory) and
is excluded from clean-room replay by WORKSPACE_ANCHORED_IGNORES.
"""

from __future__ import annotations

from pathlib import Path

from services.job_runner import gate_p2_faults

FAULT_FIXTURE = Path("tests/fixtures/phase2-gate-faults/item_mismatch.json")
ATTEMPT = (
    Path(
        "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
        "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
    )
)


def test_policy_follows_cascade_tracked_current_version() -> None:
    result = Path(ATTEMPT / "phase-2" / "gate-result.json")
    assert result.is_file(), "workspace-anchored: attempt dir must exist"
    import json  # noqa: PLC0415

    gate_version = json.loads(result.read_bytes())["gate_version"]
    assert gate_version != "v1"
    assert Path(f"config/gates/phase-2-{gate_version}.json") == gate_p2_faults.POLICY
    assert (
        ATTEMPT / "gate-cascade" / "receipts" / f"phase-2-{gate_version}.json"
    ) == gate_p2_faults.RECEIPT


def test_cli_detects_fault_at_current_frozen_chain_state(capsys) -> None:
    code = gate_p2_faults.run_fault_cli(FAULT_FIXTURE, gate_p2_faults.POLICY)
    captured = capsys.readouterr()
    assert code == 0
    assert "detected=true" in captured.out
    assert "baseline=PASS" in captured.out


def test_cli_gate_p2_error_is_typed_fault_error_not_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    stale_policy = Path("config/gates/phase-2-v1.json")
    assert stale_policy.is_file()
    monkeypatch.setattr(gate_p2_faults, "POLICY", stale_policy)
    code = gate_p2_faults.run_fault_cli(FAULT_FIXTURE, stale_policy)
    captured = capsys.readouterr()
    assert code == 2
    assert "fault-error" in captured.err
    assert "freeze-receipt-binding-drift" in captured.err
    assert "Traceback" not in captured.err


def test_cli_malformed_fault_spec_is_typed_refusal(tmp_path: Path, capsys) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    code = gate_p2_faults.run_fault_cli(broken, gate_p2_faults.POLICY)
    captured = capsys.readouterr()
    assert code == 2
    assert "fault-unreadable" in captured.err

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2]")
    code = gate_p2_faults.run_fault_cli(not_object, gate_p2_faults.POLICY)
    captured = capsys.readouterr()
    assert code == 2
    assert "fault-unreadable" in captured.err
