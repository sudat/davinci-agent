"""Offline synthesis of the Phase-3 gate evidence tree (Todo 62 QA harness).

Builds the complete A/B evidence shape WITHOUT Resolve or ffmpeg: the A/B
manifests, profiles, snapshots, and registry compile for real (offline),
while render files, parity reports, the Phase-2 regression gate result,
and the Builder scan record are synthesized through the same strict models
the live gate writes. One ``FaultKnobs`` defect is injected per run; the
REAL evaluator must detect it from recomputed evidence (the baseline with
no knob must PASS every criterion).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.contracts.serialization import canonical_json_bytes
from services.foundation_io import atomic_write
from services.job_runner.gate_p3_ab import (
    AbPlan,
    compile_ab,
    structure_rows,
    write_ab_evidence,
)
from services.job_runner.gate_p3_models import (
    AB_DIR_NAME,
    BUILDS_DIR_NAME,
    OBSERVATION_NAME,
    PARITY_REPORT_NAME,
    P3BuildObservation,
    P3GateObservation,
    P3PresentationHashes,
    P3QcRow,
    P3RenderBinding,
    P3ScanRecord,
    SideName,
    SnapshotId,
)
from services.job_runner.gate_p3_parity_fake import parity_report, seal
from services.job_runner.gate_p3_regression_fake import synth_regression
from services.job_runner.gate_p3_scan import default_channel_roots
from services.presentation.manifest import PresentationManifest

FAULTS: Final = (
    "editorial_structure_drift",
    "one_profile_only",
    "channel_branch",
    "stale_asset",
    "fallback_to_manual",
    "rights_issue",
    "phase4_import",
)
EXPECTED_CODES: Final[dict[str, str]] = {
    "editorial_structure_drift": "ab-structure-drift",
    "one_profile_only": "one-profile-only-success",
    "channel_branch": "channel-branch-present",
    "stale_asset": "stale-asset",
    "fallback_to_manual": "fallback-to-manual",
    "rights_issue": "rights-issue",
    "phase4_import": "phase4-import-attempted",
}
SNAPSHOTS: Final[tuple[SnapshotId, SnapshotId]] = ("p3-brand-a", "p3-brand-b")
SIDE_NAMES: Final[tuple[SideName, SideName]] = ("preview", "final")
PLANTED_BRANCH: Final = (
    b"def render_for(channel_id: str) -> int:\n"
    b"    if channel_id == 'brand-b':\n"
    b"        return 1\n"
    b"    return 0\n"
)


@dataclass(frozen=True, slots=True)
class FaultKnobs:
    fault: str = ""


@dataclass(frozen=True, slots=True)
class SynthTree:
    evidence: Path
    observation: P3GateObservation
    knobs: FaultKnobs


def _drift_manifest_b(ab_dir: Path) -> None:
    path = ab_dir / "manifest-p3-brand-b.json"
    manifest = PresentationManifest.model_validate_json(path.read_bytes())
    track = manifest.editorial.tracks[0]
    item = track.items[0]
    drifted_item = item.model_copy(update={"record_end": item.record_end + 1})
    drifted_track = track.model_copy(
        update={"items": (drifted_item, *track.items[1:])}
    )
    drifted = manifest.model_copy(
        update={
            "editorial": manifest.editorial.model_copy(
                update={"tracks": (drifted_track, *manifest.editorial.tracks[1:])}
            )
        }
    )
    atomic_write(path, seal(drifted).canonical_bytes())


def _synth_build(
    evidence: Path, plan: AbPlan, snapshot_id: SnapshotId, knobs: FaultKnobs
) -> P3BuildObservation:
    from services.job_runner.gate_p3_live import ROUTE  # noqa: PLC0415

    targeted = snapshot_id == "p3-brand-b"
    work = evidence / BUILDS_DIR_NAME / snapshot_id
    work.mkdir(parents=True, exist_ok=True)
    manifest = plan.manifests[snapshot_id]
    derivatives = {
        "audio_mix": hashlib.sha256(b"synthetic-mix").hexdigest(),
        "overlay": manifest.assets["overlay"].sha256,
    }
    if knobs.fault == "stale_asset" and targeted:
        derivatives["overlay"] = "0" * 64
    renders: list[P3RenderBinding] = []
    for side in SIDE_NAMES:
        render = work / f"render-{side}.mp4"
        render.write_bytes(f"synthetic-{snapshot_id}-{side}".encode())
        renders.append(
            P3RenderBinding(
                side=side,
                path=str(render),
                sha256=hashlib.sha256(render.read_bytes()).hexdigest(),
            )
        )
    payload = parity_report(
        plan, snapshot_id, derivatives, break_parity=knobs.fault == "one_profile_only" and targeted
    )
    atomic_write(
        work / PARITY_REPORT_NAME,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    )
    rights = {
        binding.asset_id: "" for binding in plan.profiles[snapshot_id].asset_bindings
    }
    if knobs.fault == "rights_issue" and targeted:
        rights[f"{snapshot_id}:tone"] = "expired"
    observation = P3BuildObservation(
        snapshot_id=snapshot_id,
        work_dir=str(work),
        route="manual" if (knobs.fault == "fallback_to_manual" and targeted) else ROUTE,
        structure=structure_rows(plan.timeline_ir),
        presentation=P3PresentationHashes(
            manifest_sha256=manifest.manifest_sha256,
            profile_snapshot_sha256=manifest.profile_snapshot_sha256,
            asset_sha256={kind: ref.sha256 for kind, ref in manifest.assets.items()},
        ),
        renders=tuple(renders),
        parity_passed=bool(payload["passed"]),
        qc=tuple(
            P3QcRow(
                side=side,
                integrated_mlufs=-16000,
                peak_mb=-1200,
                channels=2,
                issues=(),
            )
            for side in SIDE_NAMES
        ),
        readback_verified=True,
        used_asset_sha256=derivatives,
        rights_reasons=rights,
    )
    atomic_write(work / OBSERVATION_NAME, canonical_json_bytes(observation))
    return observation


def synthesize_evidence(root: Path, knobs: FaultKnobs | None = None) -> SynthTree:
    knobs = knobs or FaultKnobs()
    evidence = root / "phase-3"
    plan = compile_ab()
    ab_dir = write_ab_evidence(evidence / AB_DIR_NAME, plan)
    if knobs.fault == "editorial_structure_drift":
        _drift_manifest_b(ab_dir)
    builds = tuple(_synth_build(evidence, plan, snapshot_id, knobs) for snapshot_id in SNAPSHOTS)
    regression = synth_regression(evidence)
    roots = tuple(path.as_posix() for path in default_channel_roots())
    phase4: tuple[str, ...] = ()
    if knobs.fault == "channel_branch":
        planted = evidence / "planted" / "builder_branch.py"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(PLANTED_BRANCH)
        roots = (*roots, planted.as_posix())
    if knobs.fault == "phase4_import":
        phase4 = ("services.travel.scene_detection",)
    scan = P3ScanRecord(
        channel_roots=roots, channel_findings=(), phase4_modules=phase4
    )
    observation = P3GateObservation(builds=builds, regression=regression, scan=scan)
    atomic_write(evidence / OBSERVATION_NAME, canonical_json_bytes(observation))
    return SynthTree(evidence=evidence, observation=observation, knobs=knobs)


__all__ = [
    "EXPECTED_CODES",
    "FAULTS",
    "FaultKnobs",
    "SynthTree",
    "synthesize_evidence",
]
