from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
LEGACY_REFERENCE_COUNTS: Final[tuple[tuple[str, int], ...]] = (
    (".gitignore", 1),
    ("MEMORY.md", 1),
    ("cockpit/tests/e2e/gate-v43-4a-checklist.ts", 1),
    ("docs/prd/PRD_v4.4.md", 1),
    ("docs/system-overview.html", 1),
    ("video-pipeline/capabilities/mcp-coverage/dispositions.json", 952),
    (
        "video-pipeline/capabilities/v4.3/runs/gate-v43-1/backend-transition.json",
        2,
    ),
    ("video-pipeline/capabilities/v4.4/live-runs/v44-1/SUMMARY.txt", 1),
    (
        "video-pipeline/capabilities/v4.4/product-proof/v44-0/asr-alignment-v2.json",
        1,
    ),
    (
        "video-pipeline/capabilities/v4.4/product-proof/v44-0/SUMMARY.txt",
        1,
    ),
    ("video-pipeline/config/toolchains/phase-0a-v1.json", 12),
    ("video-pipeline/config/toolchains/phase-0b-v1.json", 15),
    ("video-pipeline/config/toolchains/phase-0c-v1.json", 17),
    ("video-pipeline/config/toolchains/phase-1-technical-v1.json", 31),
    ("video-pipeline/config/toolchains/phase-2-v1.json", 33),
    ("video-pipeline/config/toolchains/phase-3-v1.json", 34),
    ("video-pipeline/config/toolchains/pins/whisper-ja.json", 9),
    ("video-pipeline/services/execution/preflight.py", 1),
    ("video-pipeline/services/fixtures/materialize.py", 2),
    ("video-pipeline/services/job_runner/gate_p3_regression.py", 1),
    ("video-pipeline/services/job_runner/gate_phase1.py", 2),
    ("video-pipeline/services/job_runner/gate_phase2.py", 1),
    ("video-pipeline/services/metrics/v44_asr_measurement.py", 1),
    ("video-pipeline/services/qa/run_todo.py", 2),
    ("video-pipeline/services/release/replay.py", 1),
    ("video-pipeline/tests/analyze/test_visual_minimum.py", 1),
    ("video-pipeline/tests/capabilities/test_v44_frozen_evidence.py", 1),
    ("video-pipeline/tests/fixtures/test_phase0a.py", 1),
    ("video-pipeline/tests/fixtures/test_refreeze_versions.py", 1),
    ("video-pipeline/tests/fixtures/v44/transcript-artifact.json", 3),
    ("video-pipeline/tests/gates/test_control_plane_inputs.py", 1),
    ("video-pipeline/tests/gates/test_phase0a_policy.py", 2),
    ("video-pipeline/tests/gates/test_phase0c_policy.py", 1),
    ("video-pipeline/tests/gates/test_phase1_inputs.py", 1),
    ("video-pipeline/tests/gates/test_phase2_inputs.py", 1),
    ("video-pipeline/tests/gates/test_phase3_inputs.py", 1),
    ("video-pipeline/tests/metrics/test_v44_asr_measurement.py", 1),
    ("video-pipeline/tests/normalize/test_probe_budget.py", 1),
    ("video-pipeline/tests/phase0a/test_fixture_contract.py", 1),
    ("video-pipeline/tests/phase0b/test_freeze.py", 1),
    ("video-pipeline/tests/phase0c/test_freeze.py", 1),
    ("video-pipeline/tests/phase1/test_gate.py", 1),
    ("video-pipeline/tests/phase2/test_gate.py", 1),
    ("video-pipeline/tests/phase3/test_gate_live.py", 1),
    ("video-pipeline/tests/presentation/test_audio_profile.py", 1),
    ("video-pipeline/tests/presentation/test_color_profile.py", 1),
    ("video-pipeline/tests/presentation/test_overlay_paths.py", 1),
    ("video-pipeline/tests/presentation/test_preview_final_parity.py", 1),
    ("video-pipeline/tests/qa/probes.py", 5),
    ("video-pipeline/tests/qa/todo-13.json", 3),
    ("video-pipeline/tests/qa/todo-14.json", 3),
    ("video-pipeline/tests/qa/todo-15.json", 8),
    ("video-pipeline/tests/qa/todo-16.json", 10),
    ("video-pipeline/tests/qa/todo-17.json", 15),
    ("video-pipeline/tests/qa/todo-18.json", 12),
    ("video-pipeline/tests/qa/todo-19.json", 8),
    ("video-pipeline/tests/qa/todo-2.json", 5),
    ("video-pipeline/tests/qa/todo-21.json", 45),
    ("video-pipeline/tests/qa/todo-22.json", 38),
    ("video-pipeline/tests/qa/todo-23.json", 31),
    ("video-pipeline/tests/qa/todo-24.json", 3),
    ("video-pipeline/tests/qa/todo-25.json", 18),
    ("video-pipeline/tests/qa/todo-26.json", 1),
    ("video-pipeline/tests/qa/todo-27.json", 4),
    ("video-pipeline/tests/qa/todo-31.json", 11),
    ("video-pipeline/tests/qa/todo-32.json", 1),
    ("video-pipeline/tests/qa/todo-54.json", 2),
    ("video-pipeline/tests/qa/todo-6.json", 3),
    ("video-pipeline/tests/qa/todo-62.json", 2),
    ("video-pipeline/tests/qa/todo-7.json", 1),
    ("video-pipeline/tests/tooling/conftest.py", 3),
    ("video-pipeline/tests/tooling/test_source_snapshot.py", 1),
    ("video-pipeline/tests/work_init_support.py", 4),
)


def test_tracked_omo_references_match_the_closed_legacy_set() -> None:
    # Given: Git's complete list of files tracked by this repository.
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    marker = b".omo" + b"/"

    # When: every occurrence of the disposable-work path is counted by file.
    actual = Counter(
        {
            relative.as_posix(): count
            for raw_path in tracked
            if raw_path
            for relative in (Path(os.fsdecode(raw_path)),)
            if (count := (REPOSITORY_ROOT / relative).read_bytes().count(marker))
        }
    )

    # Then: no tracked file or occurrence exists outside the frozen legacy set.
    expected = dict(LEGACY_REFERENCE_COUNTS)
    assert len(expected) == len(LEGACY_REFERENCE_COUNTS)
    assert actual == expected
