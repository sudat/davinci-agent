"""Offline synthesis of a complete Phase-0A gate evidence tree from fakes.

Builds the same evidence layout the live driver produces (six run reports,
restart, recovery with rebuild, capability probes, capability matrix) using
the Build-Report fake spike, so the evaluator's rejection paths (stale
evidence, restart drift, partial-timeline dependence, Stop conditions) are
demonstrable without a live Resolve.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.serialization import artifact_content_hash
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.resolve_bridge.build_report_fakes import run_fake_spike, synthetic_host_report
from services.resolve_bridge.build_report_fingerprint import timeline_fingerprint
from services.resolve_bridge.build_report_models import REPORT_NAME
from services.resolve_bridge.fixed_presentation_srt import cue_from_recipe, render_srt
from services.spike.gate_matrix import derive_matrix
from services.spike.gate_models import (
    MATRIX_NAME,
    PROBES_DIR,
    PROBES_NAME,
    RECOVERY_DIR,
    RECOVERY_NAME,
    RESTART_DIR,
    RESTART_NAME,
    RUN_COUNT,
    BindingSnapshot,
    CapabilityProbes,
    PartialBuildRecord,
    RecoveryRecord,
    RestartRecord,
)

HOST_NAME: Final = "resolve-host.json"
FIXTURE_SUBDIR: Final = "fixture"
RATE_NUM: Final = 30
RATE_DEN: Final = 1


def fake_fixture_dir(evidence: Path) -> Path:
    """Materialize the offline fixture dir (placeholder media + valid srt)."""

    fixture = evidence / FIXTURE_SUBDIR
    fixture.mkdir(parents=True, exist_ok=True)
    for name in ("source.mov", "intro.mov", "outro.mov"):
        target = fixture / name
        if not target.is_file():
            target.write_bytes(b"offline-placeholder-" + name.encode())
    return fixture


def fake_srt(manifest: Phase0AFixtureManifest, fixture: Path) -> None:
    srt = fixture / "subtitle.srt"
    if not srt.is_file():
        srt.write_bytes(render_srt(cue_from_recipe(manifest.recipe.subtitle, RATE_NUM, RATE_DEN)))

if TYPE_CHECKING:
    from services.contracts.build_report import BuildReport0A


@dataclass(frozen=True, slots=True)
class FaultKnobs:
    stale_prior_build: bool = False
    restart_drift: bool = False
    partial_dependence: bool = False
    stop_source_identity: bool = False
    stop_capability_missing: bool = False


def _rebind(report: BuildReport0A, evidence: Path, manifest_path: Path) -> BuildReport0A:
    bindings = report.bindings.model_copy(
        update={
            "host_report_path": str(evidence / HOST_NAME),
            "ffmpeg_path": str(evidence / "ffmpeg"),
            "ffprobe_path": str(evidence / "ffprobe"),
            "manifest_path": str(manifest_path),
        }
    )
    rebound = report.model_copy(update={"bindings": bindings})
    return rebound.model_copy(update={"content_hash": artifact_content_hash(rebound)})


def _run(index: int, manifest_path: Path, evidence: Path, fixture: Path) -> BuildReport0A:
    scratch = evidence / "runs" / f"run-{index}"
    scratch.mkdir(parents=True, exist_ok=True)
    spike = run_fake_spike(manifest_path, fixture, scratch)
    if index == 1:
        atomic_write(evidence / HOST_NAME, (scratch / HOST_NAME).read_bytes())
        atomic_write(evidence / "ffmpeg", (scratch / "ffmpeg").read_bytes())
        atomic_write(evidence / "ffprobe", (scratch / "ffprobe").read_bytes())
    report = _rebind(spike.report, evidence, manifest_path)
    atomic_write(scratch / REPORT_NAME, canonical_model_bytes(report))
    return report


def _restart_record(*, drift: bool) -> RestartRecord:
    before = BindingSnapshot(
        product_name="DaVinci Resolve Studio",
        version_core="21.0.4",
        build_number=5,
        version_string="21.0.4.5",
    )
    return RestartRecord(
        before=before,
        after=before.model_copy(update={"build_number": 6}) if drift else before,
        before_pid=4242,
        after_pid=8484,
        quit_at="2026-01-01T00:00:00.000+00:00",
        exited_at="2026-01-01T00:00:12.000+00:00",
        relaunch_at="2026-01-01T00:00:13.000+00:00",
        connected_at="2026-01-01T00:00:40.000+00:00",
        quit_method="apple-event-quit",
        error="",
    )


def _probes_record() -> CapabilityProbes:
    return CapabilityProbes(
        source_end_frame_raw="599.0",
        source_end_frame_type="float",
        source_end_frame_computed=600,
        probe_item_id="cut-001",
        probe_record_start=108030,
        probe_record_end=108330,
        frame_origin=108000,
        job_status_raw='{"CompletionPercentage":0,"JobStatus":"Queued"}',
        job_marks_in=108030,
        job_marks_out=108329,
    )


def _recovery_record(evidence: Path, rebuild: BuildReport0A) -> RecoveryRecord:
    partial_fp = hashlib.sha256(b"fake-partial-timeline").hexdigest()
    return RecoveryRecord(
        partial=PartialBuildRecord(
            project_name="__fvp_test__partial_fake",
            timeline_name="__fvp_test__tl__partial_fake",
            items_placed=2,
            partial_fingerprint=partial_fp,
        ),
        rebuild_report_rel=f"{RECOVERY_DIR}/rebuild/{REPORT_NAME}",
        rebuild_report_sha256=sha256_file(evidence / RECOVERY_DIR / "rebuild" / REPORT_NAME),
        rebuild_project_name="__fvp_test__rebuild_fake",
        rebuild_fingerprint=timeline_fingerprint(tuple(row.observed for row in rebuild.items)),
        rebuild_loaded_partial=False,
        partial_reread_items=2,
        partial_reread_fingerprint=partial_fp,
        partial_deleted=True,
    )


def synthesize(manifest_path: Path, evidence: Path, knobs: FaultKnobs) -> Path:
    manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
    fixture = fake_fixture_dir(evidence)
    fake_srt(manifest, fixture)
    reports: dict[int, BuildReport0A] = {}
    for index in range(1, RUN_COUNT + 1):
        reports[index] = _run(index, manifest_path, evidence, fixture)
    atomic_write(
        evidence / RESTART_DIR / RESTART_NAME,
        canonical_model_bytes(_restart_record(drift=knobs.restart_drift)),
    )
    rebuild_scratch = evidence / RECOVERY_DIR / "rebuild"
    rebuild_scratch.mkdir(parents=True, exist_ok=True)
    rebuild = _rebind(
        run_fake_spike(manifest_path, fixture, rebuild_scratch).report,
        evidence,
        manifest_path,
    )
    rebuild_path = evidence / RECOVERY_DIR / "rebuild" / REPORT_NAME
    atomic_write(rebuild_path, canonical_model_bytes(rebuild))
    probes = _probes_record()
    atomic_write(evidence / PROBES_DIR / PROBES_NAME, canonical_model_bytes(probes))
    matrix = derive_matrix(manifest, evidence, reports, probes)
    atomic_write(evidence / MATRIX_NAME, canonical_model_bytes(matrix))
    recovery = _recovery_record(evidence, rebuild)
    atomic_write(evidence / RECOVERY_DIR / RECOVERY_NAME, canonical_model_bytes(recovery))

    if knobs.stale_prior_build:
        stale = reports[3].model_copy(
            update={
                "bindings": reports[3].bindings.model_copy(
                    update={"manifest_sha256": "f" * 64}
                )
            }
        )
        atomic_write(
            evidence / "runs" / "run-3" / REPORT_NAME, canonical_model_bytes(stale)
        )
    if knobs.partial_dependence:
        items = list(rebuild.items)
        tampered_item = items[0].model_copy(
            update={
                "observed": items[0].observed.model_copy(
                    update={
                        "record_span": items[0].observed.record_span.model_copy(
                            update={"end_frame": items[0].observed.record_span.end_frame + 1}
                        )
                    }
                )
            }
        )
        items[0] = tampered_item
        dependent = rebuild.model_copy(update={"items": tuple(items)})
        dependent = dependent.model_copy(update={"content_hash": artifact_content_hash(dependent)})
        atomic_write(rebuild_path, canonical_model_bytes(dependent))
        recovery = recovery.model_copy(
            update={
                "rebuild_report_sha256": sha256_file(rebuild_path),
                "rebuild_fingerprint": timeline_fingerprint(
                    tuple(row.observed for row in dependent.items)
                ),
            }
        )
        atomic_write(evidence / RECOVERY_DIR / RECOVERY_NAME, canonical_model_bytes(recovery))
    if knobs.stop_source_identity:
        drifted_host = synthetic_host_report().model_copy(
            update={"host": synthetic_host_report().host.model_copy(update={"macos_build": "fake"})}
        )
        atomic_write(evidence / HOST_NAME, canonical_model_bytes(drifted_host))
    if knobs.stop_capability_missing:
        trimmed = matrix.model_copy(
            update={
                "capabilities": tuple(
                    entry for entry in matrix.capabilities if entry.capability != "render"
                )
            }
        )
        atomic_write(evidence / MATRIX_NAME, canonical_model_bytes(trimmed))
    return fixture
