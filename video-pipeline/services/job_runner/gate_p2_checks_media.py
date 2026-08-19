"""Media-criterion recomputation: conformance, render, QC (Phase-2 gate).

Every verdict is recomputed from the raw artifact bytes the observation
fields point at — build outputs, refusal records, render files, QC
reports — never from a driver-authored flag.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import BuildOutput
from services.build.conformance_models import ConformanceTable
from services.foundation_io import sha256_file
from services.gates.phase2 import PHASE_2_CRITERIA

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import Phase2FixtureManifest
    from services.job_runner.gate_p2_checks import CheckState
    from services.job_runner.gate_p2_models import P2FixtureObservation

C_COMPILE, C_CONFORM, C_RENDER, C_QC, C_RECOVERY = PHASE_2_CRITERIA
REFUSED_INCOMPLETE_CODES: Final = frozenset({"render-incomplete", "render-false-complete"})
FAULT_POLL_BOUND: Final = 20
COMPLETION_PERCENTAGE: Final = 100


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise TypeError(f"not a JSON object: {path}")
    return document


def _conformance_of(
    table: ConformanceTable, fixture: str, state: CheckState, criterion: str
) -> bool:
    recomputed = bool(table.items) and all(item.passed for item in table.items)
    if not recomputed or table.extra_rows:
        state.fail(
            criterion,
            "item-conformance-defect",
            f"{fixture}: recomputed conformance is not 100%",
        )
        return False
    if table.expected_items != table.observed_items or table.total_record_delta_frames != 0:
        state.fail(
            criterion, "item-conformance-defect", f"{fixture}: item/total accounting drift"
        )
        return False
    if table.expected_fingerprint != table.observed_fingerprint:
        state.fail(criterion, "item-conformance-defect", f"{fixture}: fingerprint drift")
        return False
    return True


def check_conformance(
    observation: P2FixtureObservation,
    manifest: Phase2FixtureManifest,
    state: CheckState,
) -> None:
    fixture = observation.fixture_id
    build_path = observation.fields.get("build_output")
    if build_path is not None:
        output = BuildOutput.model_validate_json(Path(build_path).read_bytes())
        state.note(C_CONFORM, sha256_file(Path(build_path)))
        _conformance_of(output.conformance, fixture, state, C_CONFORM)
        return
    false_path = observation.fields.get("false_complete")
    if false_path is not None:
        record = _load_json(Path(false_path))
        table_raw = record.get("conformance_table")
        if not isinstance(table_raw, str):
            state.fail(C_CONFORM, "readback-missing", f"{fixture}: no readback table recorded")
            return
        table = ConformanceTable.model_validate_json(table_raw.encode())
        state.note(C_CONFORM, hashlib.sha256(table_raw.encode()).hexdigest())
        _conformance_of(table, fixture, state, C_CONFORM)
        return
    if manifest.fault.expected_readback != "not-attempted":
        state.fail(C_CONFORM, "readback-missing", f"{fixture}: declared readback not evidenced")


def check_render(
    observation: P2FixtureObservation,
    manifest: Phase2FixtureManifest,
    state: CheckState,
) -> None:
    fixture = observation.fixture_id
    build_path = observation.fields.get("build_output")
    if build_path is not None:
        output = BuildOutput.model_validate_json(Path(build_path).read_bytes())
        if output.render.completion_percentage != COMPLETION_PERCENTAGE:
            state.fail(C_RENDER, "render-not-verified", f"{fixture}: CompletionPercentage != 100")
            return
        render_file = Path(output.render.output_path)
        if not render_file.is_file() or sha256_file(render_file) != output.render.output_sha256:
            state.fail(C_RENDER, "render-bytes-drift", f"{fixture}: render file/hash mismatch")
            return
        state.note(C_RENDER, output.render.output_sha256)
        return
    false_path = observation.fields.get("false_complete")
    if false_path is not None:
        record = _load_json(Path(false_path))
        code = str(record.get("render_failure_code", ""))
        if code not in REFUSED_INCOMPLETE_CODES:
            state.fail(
                C_RENDER,
                "render-refusal-untyped",
                f"{fixture}: refusal code {code!r} is not a refused-incomplete code",
            )
            return
        polls_raw = record.get("status_polls", 0)
        polls = polls_raw if isinstance(polls_raw, int) and not isinstance(polls_raw, bool) else 0
        if not 1 <= polls <= FAULT_POLL_BOUND:
            state.fail(C_RENDER, "unbounded-retry", f"{fixture}: render polls unbounded ({polls})")
            return
        state.note(C_RENDER, sha256_file(Path(false_path)))
        return
    if manifest.fault.expected_render not in ("not-attempted", "blocked"):
        state.fail(C_RENDER, "render-missing", f"{fixture}: declared render not evidenced")


def check_qc(
    observation: P2FixtureObservation,
    manifest: Phase2FixtureManifest,
    state: CheckState,
) -> None:
    fixture = observation.fixture_id
    report_path = observation.fields.get("qc_report")
    if report_path is not None:
        raw = Path(report_path).read_bytes()
        verdict = _load_json(Path(report_path)).get("verdict")
        state.note(C_QC, hashlib.sha256(raw).hexdigest())
        repeat_path = observation.fields.get("qc_report_repeat")
        if repeat_path is not None and Path(repeat_path).read_bytes() != raw:
            state.fail(C_QC, "qc-nondeterministic", f"{fixture}: repeat QC bytes differ")
        expected = manifest.fault.expected_qc
        if expected == "deterministic-after-restart" and verdict != "passed":
            state.fail(C_QC, "qc-not-passed", f"{fixture}: verdict {verdict!r} != passed")
        if expected == "typed-failure-blocks-publish":
            if verdict != "blocked":
                state.fail(C_QC, "qc-not-blocked", f"{fixture}: verdict {verdict!r} != blocked")
                return
            gates = _load_json(Path(report_path)).get("unresolved_human_gates")
            if not isinstance(gates, list) or not gates:
                state.fail(C_QC, "qc-human-gate-missing", f"{fixture}: no unresolved human gate")
        return
    status_path = observation.fields.get("qc_status")
    if status_path is None:
        state.fail(C_QC, "qc-evidence-missing", f"{fixture}: no QC evidence recorded")
        return
    state.note(C_QC, sha256_file(Path(status_path)))
    status = _load_json(Path(status_path)).get("qc")
    expected = manifest.fault.expected_qc
    if expected in ("not-attempted", "blocked") and status != expected:
        state.fail(C_QC, "qc-route-drift", f"{fixture}: qc status {status!r} != {expected!r}")


__all__ = ["check_conformance", "check_qc", "check_render"]
