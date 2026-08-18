"""Shared CLI test rig: one chain run + the resolved production policy."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.cli.bundle import load_bundle
from services.cli.run_stage import run_phase1
from tests.e2e.conftest import local_policy_file

if sys.platform != "darwin":  # pragma: no cover - fixture toolchain is macos-only
    pytest.skip("phase-1 fixture toolchain (say/TTS) is macOS-only", allow_module_level=True)

FIXTURE_ID = "p1-ref-05-review-mix"
MANIFEST_SOURCE = Path("tests/fixtures/manifests/phase-1-technical") / f"{FIXTURE_ID}.json"


@dataclass(frozen=True, slots=True)
class CliRig:
    root: Path
    bundle_file: Path
    policy_file: Path


@pytest.fixture(scope="session")
def cli_rig(tmp_path_factory: pytest.TempPathFactory) -> CliRig:
    root = tmp_path_factory.mktemp("phase1-cli")
    episode_root = root / "episode"
    episode_root.mkdir()
    shutil.copyfile(MANIFEST_SOURCE, episode_root / "manifest.json")
    outcome = run_phase1(episode_root, "PREVIEW_READY", root / "out")
    assert outcome.report.bundle_path is not None
    bundle = load_bundle(Path(outcome.report.bundle_path))
    assert bundle.fixture_only is True
    return CliRig(
        root=root,
        bundle_file=Path(outcome.report.bundle_path),
        policy_file=local_policy_file(root / "policy.json"),
    )


def instruction_file(root: Path, name: str, text: str) -> Path:
    path = root / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    return path
