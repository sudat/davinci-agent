"""Criterion recomputation for the Phase-3 A/B Profile-swap Gate (Todo 62).

Every criterion is recomputed from the raw evidence tree only: the written
A/B manifests and Timeline IR (editorial structure), the Phase-2 regression
gate result, the static Builder scan records, the frozen fixture manifests,
the golden tables, and the parent phase-2 gate result. Build and
presentation recomputation lives in ``gate_p3_build_checks``; nothing
trusts the driver's recorded verdicts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.gates import GateResult
from services.gates.phase3 import PHASE_3_CRITERIA, PHASE_3_FIXTURES
from services.job_runner.gate_p3_ab import (
    GOLDEN_DIR,
    JOB_DATE,
    JOB_TERRITORY,
    MANIFEST_DIR,
    editorial_structure_sha,
    record_span_drift,
    structure_rows,
)
from services.job_runner.gate_p3_build_checks import check_builds, check_presentation
from services.job_runner.gate_p3_models import (
    AB_DIR_NAME,
    P3GateObservation,
    P3StructRow,
)
from services.job_runner.gate_p3_scan import (
    default_channel_roots,
    resolve_repo_path,
    scan_channel_branches,
    scan_phase4_imports,
)
from services.job_runner.gate_p3_state import (
    C_PRESENTATION,
    C_REGRESSION,
    C_RIGHTS,
    C_STRUCTURE,
    CheckState,
    Mismatch,
)
from services.presentation.asset_registry import AssetRightsError, check_rights
from services.presentation.manifest import PresentationManifest

if TYPE_CHECKING:
    from services.gates import GatePolicy

LOCK_PATH: Final = Path("config/toolchains/phase-3-v1.json")
PARENT_GATE: Final = "phase-2"
PARENT_POLICY: Final = Path("config/gates/phase-2-v1.json")

__all__ = [
    "C_PRESENTATION",
    "C_REGRESSION",
    "C_RIGHTS",
    "C_STRUCTURE",
    "CheckState",
    "Mismatch",
    "check_all",
    "check_bindings",
    "check_builds",
    "check_presentation",
    "check_registry_rights",
    "check_regression",
    "check_scan",
    "check_structure",
]


def check_bindings(policy: GatePolicy, evidence: Path, state: CheckState) -> None:
    """Frozen inputs, golden tables, toolchain lock, and the parent result."""

    combined = hashlib.sha256()
    for fixture_id in PHASE_3_FIXTURES:
        combined.update(resolve_repo_path(MANIFEST_DIR / f"{fixture_id}.json").read_bytes())
    if policy.fixture_manifest_sha256 != combined.hexdigest():
        state.fail(C_STRUCTURE, "fixture-manifest-drift", "manifest bytes drift from the policy")
    golden_index = resolve_repo_path(GOLDEN_DIR / "index.json")
    if policy.golden_sha256 != sha256_file(golden_index):
        state.fail(C_STRUCTURE, "golden-index-drift", "golden index does not match the policy")
    index = json.loads(golden_index.read_bytes())
    expected = resolve_repo_path(GOLDEN_DIR / "expected.json").read_bytes()
    if index.get("expected_sha256") != hashlib.sha256(expected).hexdigest():
        state.fail(C_STRUCTURE, "golden-expected-drift", "golden tables do not match the index")
    state.note(C_STRUCTURE, hashlib.sha256(expected).hexdigest())
    if policy.toolchain_lock_sha256 != sha256_file(resolve_repo_path(LOCK_PATH)):
        state.fail(C_STRUCTURE, "toolchain-lock-drift", "toolchain lock bytes drift")
    parent = evidence.parent / PARENT_GATE / "gate-result.json"
    if not parent.is_file() or len(policy.parent_gate_result_hashes) != 1:
        state.fail(C_STRUCTURE, "parent-gate-unbound", "the phase-2 parent result is missing")
        return
    raw = parent.read_bytes()
    try:
        result = GateResult.model_validate_json(raw)
    except ValidationError:
        state.fail(C_STRUCTURE, "parent-gate-drift", "parent gate result is malformed")
        return
    if hashlib.sha256(raw).hexdigest() != policy.parent_gate_result_hashes[0] or not (
        result.passed
    ):
        state.fail(C_STRUCTURE, "parent-gate-drift", f"parent gate result drift: {parent}")
    state.note(C_STRUCTURE, sha256_file(parent))


def check_regression(observation: P3GateObservation, state: CheckState) -> None:
    """The COMPLETE Phase-2 suite must pass 100% from its own gate result."""

    regression = observation.regression
    result_path = Path(regression.result_path)
    if regression.timed_out:
        state.fail(C_REGRESSION, "phase2-regression-timeout", "the rerun exceeded its bound")
        return
    if regression.exit_code != 0:
        state.fail(
            C_REGRESSION, "phase2-regression-failed", f"rerun exited {regression.exit_code}"
        )
    if not result_path.is_file():
        state.fail(C_REGRESSION, "phase2-regression-result-missing", str(result_path))
        return
    raw = result_path.read_bytes()
    try:
        parsed = GateResult.model_validate_json(raw)
    except ValidationError:
        state.fail(C_REGRESSION, "phase2-regression-malformed", str(result_path))
        return
    failed = [row.criterion_id for row in parsed.criteria_results if not row.passed]
    if not parsed.passed or failed:
        state.fail(
            C_REGRESSION,
            "phase2-regression-failed",
            f"passed={parsed.passed} failed={failed}",
        )
    if parsed.policy_sha256 != sha256_file(resolve_repo_path(PARENT_POLICY)):
        state.fail(
            C_REGRESSION,
            "phase2-regression-policy-drift",
            "the rerun did not bind the frozen phase-2 policy",
        )
    state.note(C_REGRESSION, hashlib.sha256(raw).hexdigest())


def _manifest_rows(manifest: PresentationManifest) -> tuple[P3StructRow, ...]:
    return tuple(
        P3StructRow(
            item_id=item.item_id,
            kind=track.kind,
            track_index=track.index,
            source_start=-1,
            source_end=-1,
            record_start=item.record_start,
            record_end=item.record_end,
        )
        for track in manifest.editorial.tracks
        for item in track.items
    )


def check_structure(evidence: Path, state: CheckState) -> None:
    """Drift 0: identical Decision ids, source/record spans, item count."""

    from services.fixtures.manifest_phase3 import Phase3FixtureManifest  # noqa: PLC0415
    from services.presentation.parity_live_fixture import timeline_ir_for  # noqa: PLC0415

    ab_dir = evidence / AB_DIR_NAME
    written: object = None
    try:
        written = json.loads((ab_dir / "timeline-ir.json").read_bytes())
        brand_a = Phase3FixtureManifest.model_validate_json(
            resolve_repo_path(MANIFEST_DIR / "p3-brand-a.json").read_bytes()
        )
        timeline_ir = timeline_ir_for(brand_a)
        manifests = {
            fixture_id: PresentationManifest.model_validate_json(
                (ab_dir / f"manifest-{fixture_id}.json").read_bytes()
            )
            for fixture_id in PHASE_3_FIXTURES
        }
        golden = json.loads((resolve_repo_path(GOLDEN_DIR) / "expected.json").read_bytes())
        structure_sha = editorial_structure_sha(resolve_repo_path(MANIFEST_DIR))
    except (OSError, ValidationError, ValueError, TypeError) as error:
        state.fail(C_STRUCTURE, "structure-evidence-malformed", str(error))
        return
    if not isinstance(written, dict):
        state.fail(C_STRUCTURE, "structure-evidence-malformed", "timeline IR not an object")
        return
    rows = structure_rows(timeline_ir)
    if str(written.get("content_hash")) != timeline_ir.content_hash:
        state.fail(
            C_STRUCTURE,
            "ab-structure-drift",
            "the written timeline IR is not the frozen approved fixture IR",
        )
    drift: tuple[str, ...] = ()
    for fixture_id, manifest in manifests.items():
        if not manifest.verify_hash():
            drift += (f"{fixture_id}:manifest-seal-invalid",)
        drift += tuple(
            f"{fixture_id}:{detail}"
            for detail in record_span_drift(rows, _manifest_rows(manifest))
        )
    if drift:
        state.fail(C_STRUCTURE, "ab-structure-drift", "; ".join(drift[:4]))
    manifest_a = manifests["p3-brand-a"]
    manifest_b = manifests["p3-brand-b"]
    if manifest_a.editorial_fingerprint != manifest_b.editorial_fingerprint:
        state.fail(C_STRUCTURE, "ab-structure-drift", "manifest editorial fingerprints differ")
    invariants = golden.get("invariants", {})
    if not isinstance(invariants, dict):
        state.fail(C_STRUCTURE, "structure-golden-drift", "golden invariants malformed")
    elif invariants.get("editorial_structure_sha256") != structure_sha:
        state.fail(
            C_STRUCTURE, "structure-golden-drift", "editorial structure hash drift from golden"
        )
    state.note(
        C_STRUCTURE,
        manifest_a.editorial_fingerprint,
        manifest_b.editorial_fingerprint,
        structure_sha,
    )


def check_scan(observation: P3GateObservation, state: CheckState) -> None:
    """No channel branch in the Builder; no Phase-4 module imported."""

    scan = observation.scan
    recorded = {Path(path).as_posix() for path in scan.channel_roots}
    defaults = {path.as_posix() for path in default_channel_roots()}
    if not defaults.issubset(recorded):
        state.fail(
            C_RIGHTS,
            "channel-scan-incomplete",
            f"scan omitted declared builder modules: {sorted(defaults - recorded)[:4]}",
        )
    findings = scan_channel_branches(Path(path) for path in scan.channel_roots)
    if findings:
        state.fail(
            C_RIGHTS,
            "channel-branch-present",
            "; ".join(
                f"{finding.path}:{finding.line} {finding.snippet}" for finding in findings[:4]
            ),
        )
    phase4 = scan_phase4_imports(scan.phase4_modules)
    if phase4:
        state.fail(C_RIGHTS, "phase4-import-attempted", ", ".join(phase4[:4]))
    scan_digest = hashlib.sha256(
        "|".join(
            f"{finding.path}:{finding.line}:{finding.snippet}" for finding in findings
        ).encode()
        + "|".join(phase4).encode()
    ).hexdigest()
    state.note(C_RIGHTS, scan_digest)


def check_registry_rights(state: CheckState) -> None:
    """Recompute rights over the frozen registry; asset bytes must match."""

    from services.presentation.asset_registry import (  # noqa: PLC0415
        registry_from_phase3_manifests,
    )

    registry = registry_from_phase3_manifests(resolve_repo_path(MANIFEST_DIR))
    for entry in registry.entries:
        resolved = resolve_repo_path(Path(entry.path))
        if not resolved.is_file():
            state.fail(C_RIGHTS, "stale-asset", f"asset file missing: {entry.asset_id}")
            continue
        try:
            check_rights(
                registry,
                entry.asset_id,
                usage=entry.usage,
                territory=JOB_TERRITORY,
                at_job=JOB_DATE,
                observed_sha256=sha256_file(resolved),
            )
        except AssetRightsError as error:
            code = "stale-asset" if str(error.reason) == "changed_bytes" else "rights-issue"
            state.fail(C_RIGHTS, code, f"{entry.asset_id}: {error}")
    state.note(C_RIGHTS, registry.registry_snapshot_sha256)


def check_all(policy: GatePolicy, evidence: Path, observation: P3GateObservation) -> CheckState:
    state = CheckState()
    check_bindings(policy, evidence, state)
    check_regression(observation, state)
    check_structure(evidence, state)
    check_registry_rights(state)
    check_builds(observation, state)
    check_scan(observation, state)
    check_presentation(evidence, state)
    for criterion in PHASE_3_CRITERIA:
        if not state.evidence[criterion]:
            state.fail(criterion, "evidence-incomplete", criterion)
    return state
