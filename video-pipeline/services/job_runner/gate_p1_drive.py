"""Drive the five frozen Phase-1 fixtures through the REAL Todo-45 chain.

Per fixture: two deterministic chain runs (``run_phase1`` — declared-observation
replay: eligibility, synthesized edit source, declared analyzers, director
replay, selection/edit commit, production IR, review plane, preview, bundle),
then the declared review-correction sequence through the real translator and
apply engine on BOTH runs, plus a non-declared schema-gap probe.
Approval-ingress probes (``drive_approvals``) prove the gate itself can never
mint an operator record.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

from services.cli.bundle import load_bundle, rehash_bundle_targets
from services.cli.chain import ChainError, load_episode_manifest
from services.cli.run_stage import REPORT_NAME, run_phase1
from services.config.models import (
    BudgetPolicy,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    ResolvedConfig,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.fixtures.manifest_phase1 import (
    PHASE_1_FIXTURE_IDS,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p1_approvals import drive_approvals
from services.job_runner.gate_p1_models import (
    BUNDLE_FILE,
    STORE_DIR,
    FixtureObservation,
    ReviewStepRecord,
)
from services.job_runner.gate_p1_review import (
    exercise_review_sequence,
)

POLICY_NAME: Final = "production-policy.json"


class DriveError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def local_review_policy(out_path: Path, episode_id: str) -> Path:
    """The local-only resolved snapshot declaring the review stage data class."""

    system = SystemConfig(
        schema_version="system-config-v1",
        retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
        data_classes=(
            StageDataClasses(stage="review_translate", classes=("review_instruction_text",)),
        ),
        cloud_allowlist=(),
        network=NetworkPosture(
            builder="loopback", builder_endpoint="unix:///run/davinci-agent/editorial.sock"
        ),
        path_allowlist=PathAllowlist(roots=("/video-pipeline/jobs",)),
        budget=BudgetPolicy(
            transient_max_attempts=3,
            permanent_max_attempts=1,
            blocking_human_max_attempts=1,
            max_stage_cost_units=1000,
            max_job_cost_units=10000,
        ),
    )
    resolved: ResolvedConfig = resolve(system, episode=EpisodeConfig(episode_id=episode_id))
    atomic_write(out_path, canonical_model_bytes(resolved))
    return out_path


def _run_chain(manifest_path: Path, out_dir: Path, episode_root: Path) -> OperationOutcome:
    episode_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_path, episode_root / "manifest.json")
    try:
        outcome = run_phase1(episode_root, "PREVIEW_READY", out_dir)
    except ChainError as error:
        return OperationOutcome(name="chain-run", result="chain-error", detail=str(error))
    except Exception as error:  # noqa: BLE001 (raw evidence keeps the failure type)
        return OperationOutcome(
            name="chain-run", result="chain-error", detail=f"{type(error).__name__}: {error}"
        )
    report = outcome.report
    if report.bundle_path is None:
        return OperationOutcome(
            name="chain-run", result="no-bundle", detail="stop stage produced no bundle"
        )
    return OperationOutcome(
        name="chain-run", result="ok", detail=f"{report.episode_id}:{report.stop_stage}"
    )


def drive_fixture(manifest_path: Path, work_root: Path) -> FixtureObservation:
    """Two chain runs + declared review sequence; observation records raw facts."""

    run1 = work_root / "run1"
    run2 = work_root / "run2"
    for directory in (run1, run2):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    run1_outcome = _run_chain(manifest_path, run1, work_root / "episode-run1")
    manifest = load_episode_manifest(work_root / "episode-run1")
    run2_outcome = _run_chain(manifest_path, run2, work_root / "episode-run2")
    operations = [run1_outcome, run2_outcome]
    review_steps: list[ReviewStepRecord] = []
    schema_gap_refused = False
    if run1_outcome.result == "ok" and run2_outcome.result == "ok":
        bundle = load_bundle(run1 / BUNDLE_FILE)
        rehash_bundle_targets(bundle, run1 / BUNDLE_FILE)
        operations.append(OperationOutcome(name="bundle-rehash", result="ok"))
        policy_file = local_review_policy(work_root / POLICY_NAME, manifest.fixture_id)
        review_steps, gap = exercise_review_sequence(
            manifest, run1, policy_file, work_root / "review"
        )
        schema_gap_refused = gap.result == "refused"
        operations.append(gap)
        if manifest.review_commands:
            exercise_review_sequence(
                manifest, run2, policy_file, work_root / "review-run2"
            )
    fields = {
        "run1_report": str(run1 / REPORT_NAME),
        "run2_report": str(run2 / REPORT_NAME),
        "store_dir": str(run1 / STORE_DIR),
    }
    return FixtureObservation(
        fixture_id=manifest.fixture_id,
        work_dir=str(work_root),
        run1_dir=str(run1),
        run2_dir=str(run2),
        operations=tuple(operations),
        review_steps=tuple(review_steps),
        schema_gap_refused=schema_gap_refused,
        fields=fields,
    )


def drive_all_fixtures(evidence: Path, manifest_dir: Path) -> dict[str, FixtureObservation]:
    observations: dict[str, FixtureObservation] = {}
    for fixture_id in PHASE_1_FIXTURE_IDS:
        work = evidence / "fixtures" / fixture_id
        work.mkdir(parents=True, exist_ok=True)
        observation = drive_fixture(manifest_dir / f"{fixture_id}.json", work)
        atomic_write(work / "observation.json", canonical_model_bytes(observation))
        observations[fixture_id] = observation
    return observations


__all__ = [
    "DriveError",
    "drive_all_fixtures",
    "drive_approvals",
    "drive_fixture",
    "exercise_review_sequence",
    "local_review_policy",
]  # drive_approvals/exercise_review_sequence re-exported from their home modules
