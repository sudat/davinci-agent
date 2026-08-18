"""Shared Phase-1 review-loop test rig (Todo 45).

Session-scoped: one deterministic chain run for ``p1-ref-05-review-mix``
(synthesized media, declared analyzers, director replay, commits, projection,
preview) reused by every test in the file. The production policy snapshot is
the local-only resolved config that declares the review stage's data class.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.cli.bundle import load_bundle
from services.cli.run_stage import run_phase1
from services.config.models import (
    BudgetPolicy,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    RetentionPolicy,
    StageDataClasses,
    SystemConfig,
)
from services.config.resolver import resolve
from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
from services.foundation_io import canonical_model_bytes

FIXTURE_ID = "p1-ref-05-review-mix"
MANIFEST_SOURCE = Path("tests/fixtures/manifests/phase-1-technical") / f"{FIXTURE_ID}.json"
TRANSLATOR_POLICY = Path("config/gates/phase-0c-v1.json")


@dataclass(frozen=True, slots=True)
class ReviewRig:
    root: Path
    bundle_file: Path
    policy_file: Path


def local_policy_file(path: Path) -> Path:
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
    resolved = resolve(system, episode=EpisodeConfig(episode_id=FIXTURE_ID))
    path.write_bytes(canonical_model_bytes(resolved))
    return path


@pytest.fixture(scope="session")
def rig(tmp_path_factory: pytest.TempPathFactory) -> ReviewRig:
    root = tmp_path_factory.mktemp("phase1-review-loop")
    episode_root = root / "episode"
    episode_root.mkdir()
    shutil.copyfile(MANIFEST_SOURCE, episode_root / "manifest.json")
    outcome = run_phase1(episode_root, "PREVIEW_READY", root / "out")
    assert outcome.report.bundle_path is not None
    bundle = load_bundle(Path(outcome.report.bundle_path))
    assert bundle.stage == "PREVIEW_READY"
    return ReviewRig(
        root=root,
        bundle_file=Path(outcome.report.bundle_path),
        policy_file=local_policy_file(root / "policy.json"),
    )


@pytest.fixture(scope="session")
def manifest() -> Phase1TechnicalFixtureManifest:
    return Phase1TechnicalFixtureManifest.model_validate_json(MANIFEST_SOURCE.read_bytes())
