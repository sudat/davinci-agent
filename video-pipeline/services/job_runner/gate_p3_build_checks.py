"""Build/presentation criterion recomputation (Todo 62, second half).

Both live snapshot builds must pass with parity verdicts re-derived from
the recorded side payloads under tolerances rebuilt from the frozen
fixtures; presentation hashes must differ exactly along the declared
golden dimensions. Every exit is a typed defect.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from services.gates.phase3 import PHASE_3_FIXTURES
from services.job_runner.gate_p3_ab import MANIFEST_DIR, presentation_diff
from services.job_runner.gate_p3_models import (
    AB_DIR_NAME,
    PARITY_REPORT_NAME,
    P3BuildObservation,
    P3GateObservation,
)
from services.job_runner.gate_p3_scan import resolve_repo_path
from services.job_runner.gate_p3_state import C_PRESENTATION, C_RIGHTS, CheckState
from services.presentation.manifest import PresentationManifest
from services.presentation.parity import compare_parity, tolerances_from_targets
from services.presentation.parity_models import ParitySide, ParityTolerances


def _fixture_asset_sha(fixture_id: str, kind: str) -> str:
    from services.fixtures.manifest_phase3 import Phase3FixtureManifest  # noqa: PLC0415

    manifest = Phase3FixtureManifest.model_validate_json(
        resolve_repo_path(MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )
    for asset in manifest.presentation.assets:
        if asset.kind == kind:
            return str(asset.sha256)
    raise ValueError(f"{fixture_id} carries no {kind} asset")


def _recompute_parity(report_path: Path) -> bool:
    payload = _parity_object(report_path)
    preview = ParitySide.model_validate(payload.get("preview"))
    final = ParitySide.model_validate(payload.get("final"))
    return compare_parity(preview, final, _declared_tolerances()).passed


def _parity_object(report_path: Path) -> dict[str, object]:
    payload = json.loads(report_path.read_bytes())
    if not isinstance(payload, dict):
        raise TypeError("parity report is not an object")
    return payload


def _declared_tolerances() -> ParityTolerances:
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


def check_builds(observation: P3GateObservation, state: CheckState) -> None:
    """Both live builds/QC/parity must pass; typed failures for every exit."""

    builds = {build.snapshot_id: build for build in observation.builds}
    missing = [fixture_id for fixture_id in PHASE_3_FIXTURES if fixture_id not in builds]
    if missing:
        state.fail(C_RIGHTS, "unclassified-failure", f"snapshots absent: {missing}")
        return
    healthy = [build for build in builds.values() if _check_one_build(build, state)]
    if len(healthy) == 1:
        state.fail(
            C_PRESENTATION,
            "one-profile-only-success",
            f"only {healthy[0].snapshot_id} completed both builds/QC/parity",
        )


def _check_one_build(build: P3BuildObservation, state: CheckState) -> bool:
    """Recompute one snapshot build; True when builds/QC/parity all pass."""

    defects = _build_defects(build)
    for criterion, code, detail in defects:
        state.fail(criterion, code, detail)
    if defects:
        return False
    state.note(
        C_PRESENTATION,
        *[row.sha256 for row in build.renders],
        build.presentation.manifest_sha256,
    )
    return True


def _build_defects(build: P3BuildObservation) -> tuple[tuple[str, str, str], ...]:
    """Every typed defect of one snapshot build (criterion, code, detail)."""

    label = build.snapshot_id
    defects: list[tuple[str, str, str]] = []
    if build.route == "manual":
        defects.append((C_RIGHTS, "fallback-to-manual", f"{label}: build routed to manual"))
    rights = [
        f"{asset_id}:{reason}" for asset_id, reason in build.rights_reasons.items() if reason
    ]
    if rights:
        defects.append((C_RIGHTS, "rights-issue", f"{label}: {rights}"))
    used = build.used_asset_sha256.get("overlay", "")
    if used and used != _fixture_asset_sha(label, "overlay"):
        defects.append((C_RIGHTS, "stale-asset", f"{label}: overlay bytes drifted"))
    report_path = Path(build.work_dir) / PARITY_REPORT_NAME
    if not report_path.is_file():
        defects.append(
            (C_PRESENTATION, "presentation-build-failed", f"{label}: no parity report")
        )
        return tuple(defects)
    try:
        recomputed = _recompute_parity(report_path)
    except (OSError, ValidationError, ValueError, TypeError) as error:
        defects.append((C_PRESENTATION, "presentation-build-failed", f"{label}: {error}"))
        return tuple(defects)
    if not recomputed or not build.parity_passed:
        defects.append((C_PRESENTATION, "parity-failed", f"{label}: parity did not pass"))
        return tuple(defects)
    qc_issues = [row.issues for row in build.qc if row.issues]
    if qc_issues:
        defects.append((C_PRESENTATION, "qc-failed", f"{label}: {qc_issues}"))
        return tuple(defects)
    renders_missing = [row.path for row in build.renders if not Path(row.path).is_file()]
    if renders_missing:
        defects.append(
            (C_PRESENTATION, "presentation-build-failed", f"{label}: renders missing")
        )
        return tuple(defects)
    if not build.readback_verified:
        defects.append((C_RIGHTS, "unclassified-failure", f"{label}: readback unverified"))
    return tuple(defects)


def check_presentation(evidence: Path, state: CheckState) -> None:
    """Presentation hashes differ exactly along the declared dimensions."""

    ab_dir = evidence / AB_DIR_NAME
    try:
        manifest_a = PresentationManifest.model_validate_json(
            (ab_dir / "manifest-p3-brand-a.json").read_bytes()
        )
        manifest_b = PresentationManifest.model_validate_json(
            (ab_dir / "manifest-p3-brand-b.json").read_bytes()
        )
    except (OSError, ValidationError) as error:
        state.fail(C_PRESENTATION, "presentation-build-failed", str(error))
        return
    if manifest_a.manifest_sha256 == manifest_b.manifest_sha256:
        state.fail(
            C_PRESENTATION,
            "presentation-hash-unchanged",
            "the swap produced identical presentation manifests",
        )
    unchanged = sorted(
        kind
        for kind in manifest_a.assets
        if manifest_a.assets[kind].sha256 == manifest_b.assets[kind].sha256
    )
    if unchanged:
        state.fail(
            C_PRESENTATION,
            "presentation-hash-unchanged",
            f"asset hashes unchanged across the swap: {unchanged}",
        )
    observed, declared = presentation_diff(manifest_a, manifest_b)
    if observed != declared:
        state.fail(
            C_PRESENTATION,
            "ab-diff-undeclared",
            f"undeclared={sorted(observed - declared)} missing={sorted(declared - observed)}",
        )
    state.note(C_PRESENTATION, manifest_a.manifest_sha256, manifest_b.manifest_sha256)


__all__ = ["check_builds", "check_presentation"]
