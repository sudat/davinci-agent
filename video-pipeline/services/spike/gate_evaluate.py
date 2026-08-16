"""Recompute every Phase-0A criterion from raw gate evidence.

Never trusts an authored pass flag: run reports are re-verified against the
frozen manifest, the host report, the pinned tools, and the render bytes;
restart/recovery records are cross-checked against recomputed fingerprints;
the capability matrix is re-derived and compared with the stored one. Writes
the canonical ``gate-result.json`` and returns the stop decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from pydantic import BaseModel, ValidationError

from services.contracts.build_report import BuildReport0A
from services.contracts.primitives import RationalFrameRate, Sha256
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import atomic_write, sha256_file
from services.gates import CriterionResult, EvidenceBundleRef, GateResult
from services.gates.phase0a import PHASE_0A_CRITERIA
from services.gates.serialization import canonical_gate_bytes
from services.resolve_bridge.base_cut_plan import (
    FIXTURE_MEDIA_FILES,
    expected_from_manifest,
    fixture_media_map,
)
from services.resolve_bridge.build_report_fingerprint import (
    requested_placement,
    timeline_fingerprint,
)
from services.resolve_bridge.build_report_models import VerifyMismatch, VerifyPort
from services.resolve_bridge.build_report_verify import verify_report
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN, SUBTITLE_SRT
from services.spike.gate_matrix import derive_matrix
from services.spike.gate_models import (
    MATRIX_NAME,
    RECOVERY_DIR,
    RECOVERY_NAME,
    REPORT_NAME,
    RESTART_DIR,
    RESTART_NAME,
    RESULT_NAME,
    RUN_COUNT,
    CapabilityMatrix,
    CapabilityProbes,
    RecoveryRecord,
    RestartRecord,
    run_report_path,
)
from services.spike.stop_rules import (
    STOP_CAPABILITY,
    STOP_IDENTITY,
    capability_stop_reason,
    capability_stop_reason_semantic,
    identity_stop_reason,
)

if TYPE_CHECKING:
    from services.gates import GatePolicy

RESULT_SCHEMA: Final = "gate-result-v1"
CODE_STALE: Final = "stale-prior-build-evidence"


@dataclass(frozen=True, slots=True)
class GateEvaluationInputs:
    policy: GatePolicy
    policy_sha256: str
    evidence: Path
    manifest_path: Path
    host_report_path: Path
    fixture_dir: Path
    ffmpeg_bin: Path
    ffprobe_bin: Path
    port: VerifyPort


@dataclass(frozen=True, slots=True)
class GateOutcome:
    result: GateResult
    stop_triggered: bool
    stop_criterion: str = ""
    stop_reason: str = ""
    mismatches: tuple[VerifyMismatch, ...] = ()
    run_fingerprints: tuple[str, ...] = ()
    expected_fingerprint: str = ""


@dataclass(slots=True)
class _State:
    criteria: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(PHASE_0A_CRITERIA, True)
    )
    rows: list[VerifyMismatch] = field(default_factory=list)
    evidence_sha: dict[str, list[str]] = field(
        default_factory=lambda: {criterion: [] for criterion in PHASE_0A_CRITERIA}
    )

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.rows.append(VerifyMismatch(code=code, detail=detail))

    def note(self, criterion: str, sha: str) -> None:
        if sha and sha not in self.evidence_sha[criterion]:
            self.evidence_sha[criterion].append(sha)


def _expected_fingerprint(
    manifest: Phase0AFixtureManifest, fixture_dir: Path
) -> str | None:
    try:
        media = fixture_media_map(fixture_dir)
        expected = expected_from_manifest(manifest, media)
        rate = RationalFrameRate(
            num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
        )
        return timeline_fingerprint(
            tuple(requested_placement(want, rate, FRAME_ORIGIN) for want in expected.items)
        )
    except (OSError, ValidationError):
        return None


def _load[Model: BaseModel](model: type[Model], path: Path) -> tuple[Model | None, str | None]:
    try:
        raw = path.read_bytes()
        return model.model_validate_json(raw), hashlib.sha256(raw).hexdigest()
    except (OSError, ValidationError):
        return None, None


def _frame_delta(report: BuildReport0A) -> int:
    worst = 0
    for row in report.items:
        requested, observed = row.requested, row.observed
        worst = max(
            worst,
            abs(observed.record_span.start_frame - requested.record_span.start_frame),
            abs(observed.record_span.end_frame - requested.record_span.end_frame),
            abs(observed.source.span.start_frame - requested.source.span.start_frame),
            abs(observed.source.span.end_frame - requested.source.span.end_frame),
        )
    return worst


def evaluate(inputs: GateEvaluationInputs) -> GateOutcome:
    state = _State()
    manifest_raw = inputs.manifest_path.read_bytes()
    manifest: Phase0AFixtureManifest = Phase0AFixtureManifest.model_validate_json(manifest_raw)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    host_sha: str | None
    try:
        host_sha = sha256_file(inputs.host_report_path)
    except OSError:
        host_sha = None

    _check_fixtures(inputs, manifest_sha, state)
    expected_fp = _expected_fingerprint(manifest, inputs.fixture_dir)
    if expected_fp is None:
        state.fail(
            "phase-0a-required-fixtures",
            "expected-fingerprint-underivable",
            "manifest+fixture do not yield the expected timeline fingerprint",
        )
    reports, fingerprints = _check_runs(
        inputs, manifest, manifest_sha, host_sha, expected_fp, state
    )
    _check_restart(inputs, state)
    _check_recovery(inputs, manifest, expected_fp, state)
    stop_criterion, stop_reason = _check_stops(
        inputs, manifest, reports, expected_fp, host_sha, state
    )

    for criterion in PHASE_0A_CRITERIA:
        if not state.evidence_sha[criterion]:
            state.note(criterion, manifest_sha)
    passed = all(state.criteria.values()) and not stop_criterion
    bundle = _evidence_bundle(inputs.evidence, state)
    result = GateResult(
        schema_version=RESULT_SCHEMA,
        record_type="gate_result",
        gate_id=inputs.policy.gate_id,
        gate_version=inputs.policy.gate_version,
        policy_sha256=inputs.policy_sha256,
        passed=passed,
        evidence_bundles=cast(
            "tuple[EvidenceBundleRef, ...]",
            (
                {
                    "bundle_sha256": bundle.bundle_sha256,
                    "raw_evidence_sha256s": list(bundle.raw_evidence_sha256s),
                },
            ),
        ),
        criteria_results=cast(
            "tuple[CriterionResult, ...]",
            tuple(
                {
                    "criterion_id": criterion,
                    "passed": state.criteria[criterion],
                    "raw_evidence_sha256s": list(state.evidence_sha[criterion]),
                }
                for criterion in PHASE_0A_CRITERIA
            ),
        ),
    )
    return GateOutcome(
        result=result,
        stop_triggered=bool(stop_criterion),
        stop_criterion=stop_criterion,
        stop_reason=stop_reason,
        mismatches=tuple(state.rows),
        run_fingerprints=fingerprints,
        expected_fingerprint=expected_fp or "",
    )


def _check_fixtures(
    inputs: GateEvaluationInputs,
    manifest_sha: str,
    state: _State,
) -> None:
    criterion = "phase-0a-required-fixtures"
    state.note(criterion, manifest_sha)
    if manifest_sha != inputs.policy.fixture_manifest_sha256:
        state.fail(
            criterion,
            "fixture-manifest-drift",
            f"manifest {manifest_sha} != frozen policy binding",
        )
    for name in (*FIXTURE_MEDIA_FILES.values(), SUBTITLE_SRT):
        if not (inputs.fixture_dir / name).is_file():
            state.fail(criterion, "fixture-media-missing", f"{inputs.fixture_dir / name}")


def _check_runs(
    inputs: GateEvaluationInputs,
    manifest: Phase0AFixtureManifest,
    manifest_sha: str,
    host_sha: str | None,
    expected_fp: str | None,
    state: _State,
) -> tuple[dict[int, BuildReport0A], tuple[str, ...]]:
    conformance = "phase-0a-item-conformance-100-percent"
    delta_c = "phase-0a-frame-delta-zero"
    repeat = "phase-0a-clean-build-repeatability"
    reports: dict[int, BuildReport0A] = {}
    fingerprints: list[str] = []
    run_hashes: list[str] = []
    for index in range(1, RUN_COUNT + 1):
        path = run_report_path(inputs.evidence, index)
        loaded, report_sha = _load(BuildReport0A, path)
        if loaded is None or report_sha is None:
            state.fail(repeat, "missing-run-evidence", f"run-{index} report missing/unreadable")
            state.fail(
                conformance, "missing-run-evidence", f"run-{index} report missing/unreadable"
            )
            fingerprints.append("")
            continue
        report = loaded
        reports[index] = report
        run_hashes.append(report_sha)
        for criterion in (conformance, delta_c, repeat):
            state.note(criterion, report_sha)
        if report.bindings.manifest_sha256 != manifest_sha or (
            host_sha is not None and report.bindings.host_report_sha256 != host_sha
        ):
            state.fail(conformance, CODE_STALE, f"run-{index} was built against different inputs")
        outcome = verify_report(
            report,
            manifest,
            inputs.fixture_dir,
            inputs.port,
            manifest_path=inputs.manifest_path,
            host_report_path=inputs.host_report_path,
            ffmpeg_bin=inputs.ffmpeg_bin,
            ffprobe_bin=inputs.ffprobe_bin,
        )
        if not outcome.passed:
            state.criteria[conformance] = False
            state.rows.extend(
                VerifyMismatch(code=row.code, detail=f"run-{index}: {row.detail}")
                for row in outcome.mismatches
            )
        fingerprint = timeline_fingerprint(tuple(row.observed for row in report.items))
        fingerprints.append(fingerprint)
        if expected_fp is not None and fingerprint != expected_fp:
            state.fail(
                repeat,
                "timeline-fingerprint-mismatch",
                f"run-{index}: {fingerprint} != expected {expected_fp}",
            )
        if _frame_delta(report) != 0:
            state.fail(delta_c, "frame-delta-nonzero", f"run-{index} item frame delta != 0")
    if len(reports) != RUN_COUNT:
        state.fail(repeat, "run-count-mismatch", f"{len(reports)}/{RUN_COUNT} runs present")
    return reports, tuple(fingerprints)


def _check_restart(inputs: GateEvaluationInputs, state: _State) -> None:
    criterion = "phase-0a-resolve-restart-repeatability"
    loaded, record_sha = _load(RestartRecord, inputs.evidence / RESTART_DIR / RESTART_NAME)
    if loaded is None or record_sha is None:
        state.fail(
            criterion, "missing-restart-evidence", "restart-evidence.json missing/unreadable"
        )
        return
    record = loaded
    state.note(criterion, record_sha)
    if record.error:
        state.fail(criterion, "restart-relaunch-error", record.error)
    if (record.before.product_name, record.before.version_core, record.before.build_number) != (
        record.after.product_name,
        record.after.version_core,
        record.after.build_number,
    ):
        state.fail(
            criterion,
            "restart-binding-drift",
            f"before {record.before.version_core}b{record.before.build_number} != "
            f"after {record.after.version_core}b{record.after.build_number}",
        )
    if record.before_pid is not None and record.before_pid == record.after_pid:
        state.fail(
            criterion,
            "restart-process-unchanged",
            f"pid {record.before_pid} survived the restart",
        )


def _check_recovery(
    inputs: GateEvaluationInputs,
    manifest: Phase0AFixtureManifest,
    expected_fp: str | None,
    state: _State,
) -> None:
    criterion = "phase-0a-partial-timeline-independent-rerun"
    loaded, record_sha = _load(RecoveryRecord, inputs.evidence / RECOVERY_DIR / RECOVERY_NAME)
    if loaded is None or record_sha is None:
        state.fail(
            criterion, "missing-recovery-evidence", "recovery-evidence.json missing/unreadable"
        )
        return
    record = loaded
    state.note(criterion, record_sha)
    rebuild_loaded, rebuild_sha = _load(
        BuildReport0A, inputs.evidence / RECOVERY_DIR / "rebuild" / REPORT_NAME
    )
    if rebuild_loaded is None or rebuild_sha is None:
        state.fail(
            criterion, "missing-rebuild-report", "recovery rebuild report missing/unreadable"
        )
        return
    rebuild = rebuild_loaded
    state.note(criterion, rebuild_sha)
    if rebuild_sha != record.rebuild_report_sha256:
        state.fail(criterion, "recovery-report-hash-drift", "recorded rebuild hash mismatch")
    if record.rebuild_project_name == record.partial.project_name:
        state.fail(criterion, "partial-project-reused", "rebuild reused the partial project")
    if record.rebuild_loaded_partial:
        state.fail(
            criterion,
            "partial-project-opened-by-rebuild",
            "the rebuild loaded the abandoned partial project",
        )
    if record.partial.partial_fingerprint == expected_fp:
        state.fail(
            criterion,
            "partial-build-suspicious",
            "partial fingerprint equals the full-build fingerprint",
        )
    if record.partial_reread_items != record.partial.items_placed or (
        record.partial_reread_fingerprint != record.partial.partial_fingerprint
    ):
        state.fail(
            criterion,
            "partial-timeline-extended",
            "post-rebuild reread of the partial project differs from the injection record",
        )
    if not record.partial_deleted:
        state.fail(criterion, "partial-project-not-deleted", "partial project still in library")
    if expected_fp is not None:
        fingerprint = timeline_fingerprint(tuple(row.observed for row in rebuild.items))
        if fingerprint != record.rebuild_fingerprint:
            state.fail(
                criterion,
                "recovery-record-fingerprint-drift",
                "recorded rebuild fingerprint != recomputed",
            )
        if fingerprint != expected_fp:
            state.fail(
                criterion,
                "partial-timeline-dependence",
                f"rebuild fingerprint {fingerprint} != expected {expected_fp}",
            )
    outcome = verify_report(
        rebuild,
        manifest,
        inputs.fixture_dir,
        inputs.port,
        manifest_path=inputs.manifest_path,
        host_report_path=inputs.host_report_path,
        ffmpeg_bin=inputs.ffmpeg_bin,
        ffprobe_bin=inputs.ffprobe_bin,
    )
    if not outcome.passed:
        state.criteria[criterion] = False
        state.rows.extend(
            VerifyMismatch(code=row.code, detail=f"rebuild: {row.detail}")
            for row in outcome.mismatches
        )


def _matrix_agrees(stored: CapabilityMatrix, derived: CapabilityMatrix) -> bool:
    """Semantic comparison of two matrices, ignoring evidence-ref file hashes."""

    def semantics(matrix: CapabilityMatrix) -> tuple[object, ...]:
        return (
            matrix.schema_version,
            matrix.resolve_version,
            matrix.resolve_build,
            tuple(
                (entry.capability, entry.api_available, entry.live_verified, entry.limitations)
                for entry in matrix.capabilities
            ),
            tuple(
                (entry.finding, entry.api_available, entry.live_verified, entry.limitations)
                for entry in matrix.findings
            ),
        )

    return semantics(stored) == semantics(derived)


def _check_stops(
    inputs: GateEvaluationInputs,
    manifest: Phase0AFixtureManifest,
    reports: dict[int, BuildReport0A],
    expected_fp: str | None,
    host_sha: str | None,
    state: _State,
) -> tuple[str, str]:
    probes_loaded, probes_sha = _load(
        CapabilityProbes, inputs.evidence / "probes" / "capability-probes.json"
    )
    matrix, matrix_sha = _load(CapabilityMatrix, inputs.evidence / MATRIX_NAME)
    identity_criterion = STOP_IDENTITY
    capability_criterion = STOP_CAPABILITY
    run_host_hashes = tuple(
        reports[index].bindings.host_report_sha256
        for index in sorted(reports)
        if index <= RUN_COUNT
    )
    reason = identity_stop_reason(host_sha, run_host_hashes, expected_fp)
    if reason is not None:
        state.fail(identity_criterion, "stop-source-identity-unverifiable", reason)
        return identity_criterion, reason
    state.note(identity_criterion, host_sha or "")
    if probes_loaded is None or probes_sha is None or len(reports) != RUN_COUNT:
        reason = "capability evidence incomplete: probes or runs missing"
        state.fail(capability_criterion, "stop-mvp-capability-unavailable", reason)
        return capability_criterion, reason
    probes = probes_loaded
    state.note(capability_criterion, probes_sha)
    if matrix is not None:
        state.note(capability_criterion, matrix_sha or "")
        reason = capability_stop_reason_semantic(matrix)
        if reason is not None:
            state.fail(capability_criterion, "stop-mvp-capability-unavailable", reason)
            return capability_criterion, reason
    derived = derive_matrix(manifest, inputs.evidence, reports, probes)
    if matrix is not None and not _matrix_agrees(matrix, derived):
        reason = "stored capability matrix disagrees with recomputed live evidence"
        state.fail(capability_criterion, "capability-matrix-unverifiable", reason)
        return capability_criterion, reason
    reason = capability_stop_reason(derived, inputs.evidence)
    if reason is not None:
        state.fail(capability_criterion, "stop-mvp-capability-unavailable", reason)
        return capability_criterion, reason
    return "", ""


def _evidence_bundle(evidence: Path, state: _State) -> EvidenceBundleRef:
    inventory: dict[str, str] = {}
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file() and path.name != RESULT_NAME:
                inventory[path.relative_to(evidence).as_posix()] = sha256_file(path)
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    bundle_sha = hashlib.sha256(payload).hexdigest()
    fallback = state.evidence_sha[PHASE_0A_CRITERIA[0]] or ["0" * 64]
    raw = tuple(inventory.values()) or tuple(fallback)
    return EvidenceBundleRef(
        bundle_sha256=bundle_sha, raw_evidence_sha256s=cast("tuple[Sha256, ...]", raw)
    )


def write_result(outcome: GateOutcome, evidence: Path) -> Path:
    target = evidence / RESULT_NAME
    atomic_write(target, canonical_gate_bytes(outcome.result))
    return target
