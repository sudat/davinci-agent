"""A/B profile-swap evidence for the live replay (Todo 67 / F3).

The SAME frozen editorial Timeline IR builds under profile A then B; the
swap evidence records structural equality (identical structure rows and
sha) plus the declared presentation differences (manifest, snapshot, and
asset/derivative hashes that must change between profiles).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.job_runner.gate_p3_ab import structure_rows
from services.release.live_models import (
    PROFILE_SNAPSHOT_IDS,
    ProfileBuildEvidence,
    ProfileId,
    ProfileSwapEvidence,
)
from services.release.live_plan import frozen_ab_plan
from services.release.staging import sha256_bytes

if TYPE_CHECKING:
    from services.job_runner.gate_p3_models import P3StructRow
    from services.release.live_builds import ConnectionLike
    from services.release.live_flow import LiveSeams


def _structure_bytes(rows: tuple[P3StructRow, ...]) -> bytes:
    payload = [row.model_dump(mode="json") for row in rows]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


SWAP_HASH_FIELDS: tuple[str, ...] = ("manifest_sha256", "profile_snapshot_sha256")
TWO_PROFILES = 2


def _presentation_differences(
    first: ProfileBuildEvidence, second: ProfileBuildEvidence
) -> list[str]:
    differences = [
        field
        for field in SWAP_HASH_FIELDS
        if getattr(first, field) != getattr(second, field)
    ]
    differences.extend(
        f"asset:{kind}"
        for kind in sorted(set(first.asset_sha256) | set(second.asset_sha256))
        if first.asset_sha256.get(kind) != second.asset_sha256.get(kind)
    )
    differences.extend(
        f"derivative:{kind}"
        for kind in sorted(set(first.derivatives) | set(second.derivatives))
        if first.derivatives.get(kind) != second.derivatives.get(kind)
    )
    return differences


def profile_swap(
    seams: LiveSeams,
    connection: dict[str, ConnectionLike],
    profiles: tuple[ProfileId, ...],
    evidence_root: Path,
) -> tuple[ProfileSwapEvidence, Path | None, list[str]]:
    plan = frozen_ab_plan()
    rows = structure_rows(plan.timeline_ir)
    structure_sha = sha256_bytes(_structure_bytes(rows))
    failures: list[str] = []
    evidences: list[ProfileBuildEvidence] = []
    final_render: Path | None = None
    for profile in profiles:
        snapshot = PROFILE_SNAPSHOT_IDS[profile]
        manifest = plan.manifests[snapshot]
        work = evidence_root / "profile-swap" / snapshot
        try:
            built = seams.profile_build(connection["conn"], profile, work)
        except Exception as error:  # noqa: BLE001 (typed honest failure)
            failures.append(f"profile_build_failed:{snapshot}:{type(error).__name__}")
            continue
        evidences.append(
            ProfileBuildEvidence(
                profile=profile,
                snapshot_id=snapshot,
                structure_sha256=structure_sha,
                structure_rows=len(rows),
                manifest_sha256=manifest.manifest_sha256,
                profile_snapshot_sha256=manifest.profile_snapshot_sha256,
                asset_sha256={kind: ref.sha256 for kind, ref in manifest.assets.items()},
                derivatives=dict(built.derivatives),
                render_path=str(built.render_path),
                render_sha256=sha256_file(built.render_path),
            )
        )
        final_render = built.render_path
    structural_equal = len({row.structure_sha256 for row in evidences}) <= 1
    differences: list[str] = []
    if len(evidences) == TWO_PROFILES:
        differences = _presentation_differences(evidences[0], evidences[1])
    swap_passed = (
        not failures
        and bool(evidences)
        and structural_equal
        and (len(evidences) < TWO_PROFILES or bool(differences))
    )
    if not swap_passed and not differences and len(evidences) == TWO_PROFILES:
        failures.append("presentation_unchanged_between_profiles")
    evidence = ProfileSwapEvidence(
        profiles=tuple(evidences),
        structural_equal=structural_equal,
        presentation_differences=tuple(differences),
        passed=swap_passed,
    )
    return evidence, final_render, failures


__all__ = ["profile_swap"]
