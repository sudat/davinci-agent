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
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes
from services.foundation_io import atomic_write
from services.gates import GateResult
from services.gates.phase2 import PHASE_2_CRITERIA
from services.gates.serialization import canonical_gate_bytes
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
    REGRESSION_DIR_NAME,
    P3BuildObservation,
    P3GateObservation,
    P3PresentationHashes,
    P3QcRow,
    P3RegressionObservation,
    P3RenderBinding,
    P3ScanRecord,
    SideName,
    SnapshotId,
)
from services.job_runner.gate_p3_scan import default_channel_roots
from services.presentation.manifest import PresentationManifest
from services.presentation.parity import compare_parity, tolerances_from_targets
from services.presentation.parity_live_fixture import (
    cue_observations,
    item_observations,
    placement_observation,
)
from services.presentation.parity_models import ParitySide, ParityTolerances

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
PHASE_2_POLICY: Final = Path("config/gates/phase-2-v1.json")
ATTEMPT: Final = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
PARENT: Final = "phase-2"
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


@dataclass(slots=True)
class SynthTree:
    evidence: Path
    observation: P3GateObservation
    knobs: FaultKnobs


def _tolerances() -> ParityTolerances:
    from services.presentation.audio_models import AudioTargets  # noqa: PLC0415
    from services.presentation.audio_profile import (  # noqa: PLC0415
        DEFAULT_CHANNELS,
        DEFAULT_LOUDNESS_TARGET_MLUFS,
        DEFAULT_LOUDNESS_TOLERANCE_MLUFS,
        DEFAULT_MAX_PEAK_MB,
    )

    return tolerances_from_targets(
        AudioTargets(
            loudness_target_mlufs=DEFAULT_LOUDNESS_TARGET_MLUFS,
            loudness_tolerance_mlufs=DEFAULT_LOUDNESS_TOLERANCE_MLUFS,
            max_peak_mb=DEFAULT_MAX_PEAK_MB,
            channels=DEFAULT_CHANNELS,
            sample_rate_hz=48000,
        )
    )


def _seal(manifest: PresentationManifest) -> PresentationManifest:
    draft = manifest.model_copy(update={"manifest_sha256": GENESIS_SHA256})
    return draft.model_copy(update={"manifest_sha256": draft.content_hash()})


def _side_payload(
    plan: AbPlan, snapshot_id: SnapshotId, derivatives: dict[str, str], side: SideName
) -> dict[str, object]:
    manifest = plan.manifests[snapshot_id]
    return {
        "side": side,
        "manifest_sha256": manifest.manifest_sha256,
        "profile_snapshot_sha256": manifest.profile_snapshot_sha256,
        "items": [row.model_dump(mode="json") for row in item_observations(manifest)],
        "assets": {kind: ref.sha256 for kind, ref in manifest.assets.items()},
        "placements": placement_observation(manifest).model_dump(mode="json"),
        "cues": [
            row.model_dump(mode="json") for row in cue_observations(manifest, plan.timeline_ir)
        ],
        "derivatives": derivatives,
        "audio": {
            "duration_samples": 960000,
            "integrated_mlufs": -16000,
            "peak_mb": -1200,
            "channels": 2,
        },
        "color": {"color_space": "bt709", "color_transfer": "bt709", "region_means": {}},
    }


def _parity_report(
    plan: AbPlan, snapshot_id: SnapshotId, derivatives: dict[str, str], *, break_parity: bool
) -> dict[str, object]:
    preview = ParitySide.model_validate(
        _side_payload(plan, snapshot_id, derivatives, "preview")
    )
    final = ParitySide.model_validate(_side_payload(plan, snapshot_id, derivatives, "final"))
    if break_parity:
        final = final.model_copy(
            update={"derivatives": {"audio_mix": "0" * 64, "overlay": "0" * 64}}
        )
    report = compare_parity(preview, final, _tolerances())
    return {
        "passed": report.passed,
        "manifest_sha256": report.manifest_sha256,
        "derivatives": report.derivative_hashes,
        "dims_compared": list(report.dims_compared),
        "mismatches": [row.model_dump(mode="json") for row in report.mismatches],
        "preview": preview.model_dump(mode="json"),
        "final": final.model_dump(mode="json"),
    }


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
    atomic_write(path, _seal(drifted).canonical_bytes())


def _synth_regression(evidence: Path) -> P3RegressionObservation:
    policy_sha = hashlib.sha256(
        (Path(__file__).resolve().parents[2] / PHASE_2_POLICY).read_bytes()
    ).hexdigest()
    bundle = hashlib.sha256(b"synthetic-phase2").hexdigest()
    result = GateResult.model_validate(
        {
            "schema_version": "gate-result-v1",
            "record_type": "gate_result",
            "gate_id": "phase-2",
            "gate_version": "v1",
            "policy_sha256": policy_sha,
            "passed": True,
            "evidence_bundles": [
                {"bundle_sha256": bundle, "raw_evidence_sha256s": [bundle]}
            ],
            "criteria_results": [
                {
                    "criterion_id": criterion,
                    "passed": True,
                    "raw_evidence_sha256s": [hashlib.sha256(criterion.encode()).hexdigest()],
                }
                for criterion in PHASE_2_CRITERIA
            ],
        }
    )
    reg_dir = evidence / REGRESSION_DIR_NAME
    reg_dir.mkdir(parents=True, exist_ok=True)
    raw = canonical_gate_bytes(result)
    atomic_write(reg_dir / "gate-result.json", raw)
    return P3RegressionObservation(
        evidence_dir=str(reg_dir),
        exit_code=0,
        timed_out=False,
        result_path=str(reg_dir / "gate-result.json"),
        result_sha256=hashlib.sha256(raw).hexdigest(),
        policy_sha256=policy_sha,
        criteria_passed=dict.fromkeys(PHASE_2_CRITERIA, True),
        passed=True,
    )


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
    payload = _parity_report(
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
    parent_dir = root / PARENT
    parent_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ATTEMPT / PARENT / "gate-result.json", parent_dir / "gate-result.json")
    plan = compile_ab()
    ab_dir = write_ab_evidence(evidence / AB_DIR_NAME, plan)
    if knobs.fault == "editorial_structure_drift":
        _drift_manifest_b(ab_dir)
    builds = tuple(_synth_build(evidence, plan, snapshot_id, knobs) for snapshot_id in SNAPSHOTS)
    regression = _synth_regression(evidence)
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
    "ATTEMPT",
    "EXPECTED_CODES",
    "FAULTS",
    "FaultKnobs",
    "SynthTree",
    "synthesize_evidence",
]
