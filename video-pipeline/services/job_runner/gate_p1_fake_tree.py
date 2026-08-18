"""Assemble the synthesized five-fixture fake evidence tree (fault harness).

Builds the per-fixture runs (via ``gate_p1_fakes``) plus the approvals
observation under one evidence root; each fault knob injects exactly one
defect into an otherwise golden-faithful tree.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

from services.foundation_io import atomic_write
from services.gates.phase1_technical import PHASE_1_TECHNICAL_FIXTURES
from services.job_runner.gate_p1_fakes import FaultKnobs, _write_run
from services.job_runner.gate_p1_models import APPROVALS_OBSERVATION, FixtureObservation


def synthesize(
    evidence: Path,
    knobs: FaultKnobs,
    goldens: Mapping[str, Mapping[str, object]],
    manifests: Mapping[str, Mapping[str, object]],
) -> dict[str, FixtureObservation]:
    if evidence.exists():
        shutil.rmtree(evidence)
    observations: dict[str, FixtureObservation] = {}
    for fixture_id in PHASE_1_TECHNICAL_FIXTURES:
        work = evidence / "fixtures" / fixture_id
        work.mkdir(parents=True)
        observations[fixture_id] = _write_run(
            work, manifests[fixture_id], goldens[fixture_id], fixture_id, knobs
        )
    approvals = evidence / APPROVALS_OBSERVATION
    approvals.mkdir(parents=True)
    probes = [
        {"name": "automation-ingress-refused",
         "result": "unexpected-success" if knobs.fault == "auto_created_operator_record"
         else "automation-refused", "detail": ""},
        {"name": "non-tty-ingress-refused", "result": "not-a-tty", "detail": ""},
        {"name": "operator-gate-fixture-refused", "result": "fixture-record", "detail": ""},
    ]
    from services.job_runner.gate_cp_models import (  # noqa: PLC0415
        GateObservation,
        OperationOutcome,
    )

    atomic_write(
        approvals / "observation.json",
        json.dumps(
            {
                "fixture_id": APPROVALS_OBSERVATION,
                "kind": "approval-ingress",
                "work_dir": str(approvals),
                "operations": probes,
                "fields": {"fixture_record_fixture_only": "true"},
            },
            sort_keys=True,
        ).encode(),
    )
    del GateObservation, OperationOutcome
    return observations


__all__ = ["synthesize"]
