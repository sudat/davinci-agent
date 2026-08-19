"""Offline synthesis of the Phase-2 gate evidence tree (Todo 54 QA harness).

Builds the full five-fixture evidence shape WITHOUT Resolve or ffmpeg:
packages and fault probes compile for real (offline), while build outputs,
renders, QC reports, and final-review artifacts are synthesized through
the same strict models the live gate writes. One ``FaultKnobs`` defect is
injected per run; the REAL evaluator must detect it from recomputed
evidence (the baseline with no knob must PASS every criterion).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p2_drive import FIXTURES_ROOT, drive_stale, drive_wrong_media
from services.job_runner.gate_p2_fake_models import (
    EXPECTED_CODES,
    FAULTS,
    FIXTURE_IDS,
    FaultKnobs,
    SynthTree,
    _build_output,
    _passed_report,
    _privacy_report,
    _render_file,
    _table,
)
from services.job_runner.gate_p2_finalize import (
    REPORT_NAME,
    REPORT_REPEAT_NAME,
    finalize_clean,
    finalize_privacy_block,
    fixture_privacy_declarations,
)
from services.job_runner.gate_p2_ir import compile_package, manifest_for, production_ir
from services.job_runner.gate_p2_models import P2FixtureObservation
from services.job_runner.gate_p2_probe import declared_view

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage


def _real_presented_record() -> str:
    return (
        json.dumps(
            {
                "record_id": "rec-synthetic-real",
                "purpose": "final",
                "target_type": "final-render",
                "target_hash": "0" * 64,
                "decision": "approve",
                "actor_id": "synthetic",
                "fixture_only": False,
                "runner_class": "operator",
                "superseded_record_id": None,
                "supersession_key": "final:synthetic",
                "timestamp_seq": 99,
                "record_hash": "0" * 64,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, sort_keys=True).encode())


def _fake_partial(
    work: Path, fixture_id: str, package: ResolvePackage, knobs: FaultKnobs
) -> tuple[list[OperationOutcome], dict[str, str]]:
    operations: list[OperationOutcome] = [OperationOutcome(name="clean-compile", result="ok")]
    fields: dict[str, str] = {"package": str(work / "package.json")}
    _write_json(
        work / "interrupt" / "interrupt.json",
        {
            "interrupted": True,
            "detail": "synthetic kill seam",
            "placed_items": 4,
            "declared_interrupt_after": 3,
            "total_placements": len(package.placements),
            "orphans": [f"__fvp_test__build_{package.content_hash[:12]}"],
        },
    )
    fields["interrupt"] = str(work / "interrupt" / "interrupt.json")
    _write_json(work / "interrupt" / "drift.json", {"drift_detected": True, "kind": "missing_item"})
    fields["drift"] = str(work / "interrupt" / "drift.json")
    render, sha = _render_file(work, fixture_id)
    fields["render_output"] = str(render)
    fields["render_sha256"] = sha
    build = _build_output(
        fixture_id, package, render, sha, break_one=knobs.fault == "item_mismatch"
    )
    build_path = work / "build" / "build-output.json"
    atomic_write(build_path, canonical_model_bytes(build))
    fields["build_output"] = str(build_path)
    operations.append(OperationOutcome(name="rebuild", result="ok"))
    report = _passed_report(sha)
    if knobs.fault != "missing_qc_readback":
        _write_json(work / "qc" / REPORT_NAME, json.loads(report.model_dump_json()))
        _write_json(work / "qc" / REPORT_REPEAT_NAME, json.loads(report.model_dump_json()))
        fields["qc_report"] = str(work / "qc" / REPORT_NAME)
        fields["qc_report_repeat"] = str(work / "qc" / REPORT_REPEAT_NAME)
    operations.append(
        OperationOutcome(name="qc", result=report.verdict, detail="two identical runs")
    )
    if knobs.fault != "missing_approval":
        finalized = finalize_clean(
            episode_id=fixture_id,
            render_path=render,
            build_output_sha256=sha256_file(build_path),
            conformance_fingerprint=build.timeline_fingerprint,
            qc_report=report,
            checkpoint_sha256="0" * 64,
            out_dir=work / "final-review",
        )
        fields.update(finalized)
        if knobs.fault == "synthetic_as_real_approval":
            records = Path(finalized["records_path"])
            records.write_text(records.read_text() + _real_presented_record())
        operations.append(OperationOutcome(name="final-review", result="approved-fixture-only"))
    return operations, fields


def _fake_false_complete(
    work: Path, package: ResolvePackage, knobs: FaultKnobs
) -> tuple[list[OperationOutcome], dict[str, str]]:
    operations: list[OperationOutcome] = [OperationOutcome(name="clean-compile", result="ok")]
    fields: dict[str, str] = {"package": str(work / "package.json")}
    record = {
        "readback_conformant": True,
        "readback_items": len(package.placements),
        "conformance_table": canonical_model_bytes(_table(package)).decode(),
        "render_failure_code": "render-incomplete",
        "render_failure_detail": "synthetic lying status refused below 100",
        "status_polls": 999 if knobs.fault == "unbounded_retry" else 4,
        "swept": [],
    }
    path = work / "refusal" / "false-complete.json"
    _write_json(path, record)
    fields["false_complete"] = str(path)
    operations.append(
        OperationOutcome(name="render-refusal", result="typed-failure:render-incomplete")
    )
    fields["qc_status"] = _write_status(work, "blocked", "render refused: QC not attempted")
    fields["human_route"] = _write_route(work, "render-job-review")
    return operations, fields


def _fake_privacy(
    work: Path, fixture_id: str, package: ResolvePackage
) -> tuple[list[OperationOutcome], dict[str, str]]:
    operations: list[OperationOutcome] = [OperationOutcome(name="clean-compile", result="ok")]
    fields: dict[str, str] = {"package": str(work / "package.json")}
    render, sha = _render_file(work, fixture_id)
    fields["render_output"] = str(render)
    fields["render_sha256"] = sha
    build = _build_output(fixture_id, package, render, sha)
    build_path = work / "build" / "build-output.json"
    atomic_write(build_path, canonical_model_bytes(build))
    fields["build_output"] = str(build_path)
    operations.append(OperationOutcome(name="build", result="ok"))
    report = _privacy_report(sha)
    _write_json(work / "qc" / REPORT_NAME, json.loads(report.model_dump_json()))
    fields["qc_report"] = str(work / "qc" / REPORT_NAME)
    operations.append(OperationOutcome(name="qc", result=report.verdict))
    finalized = finalize_privacy_block(
        episode_id=fixture_id,
        render_path=render,
        build_output_sha256=sha256_file(build_path),
        conformance_fingerprint=build.timeline_fingerprint,
        qc_report=report,
        declarations=fixture_privacy_declarations(manifest_for(fixture_id)),
        checkpoint_sha256="0" * 64,
        out_dir=work / "final-review",
    )
    fields.update(finalized)
    fields["human_route"] = _write_route(work, "privacy-dismissal-required")
    operations.append(OperationOutcome(name="final-review", result="refused:privacy-unresolved"))
    return operations, fields


def _write_status(work: Path, status: str, reason: str) -> str:
    path = work / "qc-status.json"
    _write_json(path, {"qc": status, "reason": reason})
    return str(path)


def _write_route(work: Path, route: str) -> str:
    path = work / "human-route.json"
    _write_json(path, {"human_route": route, "fixture_only": True})
    return str(path)


def synthesize_evidence(root: Path, knobs: FaultKnobs | None = None) -> SynthTree:
    evidence = root / "phase-2"
    knobs = knobs if knobs is not None else FaultKnobs()
    observations: dict[str, P2FixtureObservation] = {}
    for fixture_id in FIXTURE_IDS:
        work = evidence / FIXTURES_ROOT / fixture_id
        work.mkdir(parents=True, exist_ok=True)
        manifest = manifest_for(fixture_id)
        kind = manifest.fault.kind
        if kind in ("stale-capability", "same-duration-wrong-media"):
            if kind == "stale-capability":
                operations, fields, _observed = drive_stale(work, manifest)
            else:
                operations, fields, _observed = drive_wrong_media(work, manifest)
        else:
            package = compile_package(manifest)
            atomic_write(work / "package.json", canonical_model_bytes(package))
            atomic_write(work / "timeline-ir.json", canonical_model_bytes(production_ir(manifest)))
            if kind == "partial-build-restart":
                operations, fields = _fake_partial(work, fixture_id, package, knobs)
            elif kind == "false-render-complete":
                operations, fields = _fake_false_complete(work, package, knobs)
            else:
                operations, fields = _fake_privacy(work, fixture_id, package)
        observation = P2FixtureObservation(
            fixture_id=fixture_id,
            work_dir=str(work),
            operations=tuple(operations),
            fields=fields,
            declared_route=declared_view(manifest),
            manual_resolve_ui_used="true" if knobs.fault == "manual_ui_dependency" else "none",
        )
        atomic_write(work / "observation.json", canonical_model_bytes(observation))
        observations[fixture_id] = observation
    return SynthTree(evidence=evidence, observations=observations, knobs=knobs)


def synthesize_baseline(root: Path) -> SynthTree:
    return synthesize_evidence(root, FaultKnobs())


__all__ = [
    "EXPECTED_CODES",
    "FAULTS",
    "FaultKnobs",
    "SynthTree",
    "synthesize_baseline",
    "synthesize_evidence",
]
