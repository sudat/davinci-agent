"""Regression: fault-harness modules must import without the deleted attempt dir."""

from __future__ import annotations

from pathlib import Path


def test_fault_harness_modules_import_and_pin_policy() -> None:
    from services.job_runner import gate_p2_faults, gate_p3_faults  # noqa: PLC0415

    assert gate_p2_faults.POLICY == Path("config/gates/phase-2-v4.json")  # noqa: SIM300
    assert gate_p2_faults.POLICY.is_file()
    assert not hasattr(gate_p2_faults, "ATTEMPT")
    assert not hasattr(gate_p2_faults, "RECEIPT")
    assert gate_p3_faults.POLICY == Path("config/gates/phase-3-v3.json")  # noqa: SIM300
    assert gate_p3_faults.POLICY.is_file()
