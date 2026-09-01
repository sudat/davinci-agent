"""Synthetic parity evidence for the offline Phase-3 A/B builds.

Builds the per-side observation payloads and the preview/final parity
reports the fake tree writes for each snapshot: real manifests, real
tolerance models, synthetic render derivatives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.contracts.serialization import GENESIS_SHA256
from services.presentation.parity import compare_parity, tolerances_from_targets
from services.presentation.parity_live_fixture import (
    cue_observations,
    item_observations,
    placement_observation,
)
from services.presentation.parity_models import ParitySide, ParityTolerances

if TYPE_CHECKING:
    from services.job_runner.gate_p3_ab import AbPlan
    from services.job_runner.gate_p3_models import SideName, SnapshotId
    from services.presentation.manifest import PresentationManifest


def tolerances() -> ParityTolerances:
    """The declared A/B parity tolerances, derived from the audio targets."""
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


def seal(manifest: PresentationManifest) -> PresentationManifest:
    """Reseal a mutated manifest so its self-hash is valid again."""
    draft = manifest.model_copy(update={"manifest_sha256": GENESIS_SHA256})
    return draft.model_copy(update={"manifest_sha256": draft.content_hash()})


def side_payload(
    plan: AbPlan, snapshot_id: SnapshotId, derivatives: dict[str, str], side: SideName
) -> dict[str, object]:  # noqa: OBJECT_OK (heterogeneous JSON payload, verbatim moved)
    """One side's raw observation payload, recomputed from the manifest."""
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


def parity_report(
    plan: AbPlan, snapshot_id: SnapshotId, derivatives: dict[str, str], *, break_parity: bool
) -> dict[str, object]:  # noqa: OBJECT_OK (heterogeneous JSON payload, verbatim moved)
    """The preview/final parity report bytes for one snapshot build."""
    preview = ParitySide.model_validate(
        side_payload(plan, snapshot_id, derivatives, "preview")
    )
    final = ParitySide.model_validate(side_payload(plan, snapshot_id, derivatives, "final"))
    if break_parity:
        final = final.model_copy(
            update={"derivatives": {"audio_mix": "0" * 64, "overlay": "0" * 64}}
        )
    report = compare_parity(preview, final, tolerances())
    return {
        "passed": report.passed,
        "manifest_sha256": report.manifest_sha256,
        "derivatives": report.derivative_hashes,
        "dims_compared": list(report.dims_compared),
        "mismatches": [row.model_dump(mode="json") for row in report.mismatches],
        "preview": preview.model_dump(mode="json"),
        "final": final.model_dump(mode="json"),
    }


__all__ = ["parity_report", "seal", "side_payload", "tolerances"]
