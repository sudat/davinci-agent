from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from services.foundation_io import atomic_write, sha256_file
from services.gates.control_plane import PHASE_1_CONTROL_PLANE_CRITERIA
from services.job_runner.gate_cp_scenarios import combined_manifest_sha256 as _combined

PIPELINE_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = PIPELINE_ROOT / "tests" / "fixtures" / "manifests" / "control-plane"
TOOLCHAIN_LOCK = PIPELINE_ROOT / "config" / "toolchains" / "phase-0c-v2.json"


def combined_manifest_sha256() -> str:
    return _combined(MANIFEST_DIR)


def synthetic_parent_result(directory: Path) -> Path:
    payload = {
        "schema_version": "gate-result-v1",
        "record_type": "gate_result",
        "gate_id": "phase-0c",
        "gate_version": "v1",
        "policy_sha256": "1" * 64,
        "passed": True,
        "evidence_bundles": [
            {
                "bundle_sha256": "2" * 64,
                "raw_evidence_sha256s": ["3" * 64],
            }
        ],
        "criteria_results": [
            {
                "criterion_id": criterion,
                "passed": True,
                "raw_evidence_sha256s": ["4" * 64],
            }
            for criterion in PHASE_1_CONTROL_PLANE_CRITERIA[:1]
        ],
    }
    target = directory / "parent-gate-result.json"
    directory.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    atomic_write(target, canonical)
    return target


def synthetic_policy(
    directory: Path,
    *,
    parent_sha256: str | None = None,
    fixture_manifest_sha256: str | None = None,
    toolchain_sha256: str | None = None,
) -> tuple[Path, str]:
    payload = {
        "schema_version": "gate-policy-v1",
        "record_type": "gate_policy",
        "purpose": "gate_policy_freeze",
        "gate_id": "control-plane-baseline",
        "gate_version": "v1",
        "parent_gate_result_hashes": [parent_sha256 or "0" * 64],
        "criteria": list(PHASE_1_CONTROL_PLANE_CRITERIA),
        "toolchain_lock_sha256": toolchain_sha256 or sha256_file(TOOLCHAIN_LOCK),
        "fixture_manifest_sha256": fixture_manifest_sha256 or combined_manifest_sha256(),
        "golden_sha256": None,
        "prerequisite_bindings": [],
        "capability_allowlist": [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    target = directory / "policy.json"
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(target, canonical)
    return target, hashlib.sha256(canonical).hexdigest()


def run_gate_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.job_runner.run_gate", *args],
        cwd=PIPELINE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
