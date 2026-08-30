"""Task 7 evidence-verdict regressions: exit 0 requires every observed gate.

The verdict is a PURE function over the two parsed render results, the
first-run vendor counts, the rerun vendor delta, and the runner step
statuses — no constants, no metadata shortcuts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_PROBE_DIR = Path(__file__).resolve().parents[2] / "capabilities/v4.4/probes/task7-native-render"
sys.path.insert(0, str(_PROBE_DIR))
_spec = importlib.util.spec_from_file_location("t7_evidence", _PROBE_DIR / "t7_evidence.py")
t7_evidence = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["t7_evidence"] = t7_evidence
_spec.loader.exec_module(t7_evidence)  # type: ignore[union-attr]

evidence_verdict: Any = t7_evidence.evidence_verdict


def _result(
    *,
    job_id: str = "job-a",
    output_path: str = "render/out.mp4",
    sha: str = "a" * 64,
    reused: bool,
    duration: float = 1.0,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "output_path": output_path,
        "output_sha256": sha,
        "reused": reused,
        "media": {
            "video_codec": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30/1",
            "duration_seconds": duration,
            "audio_codec": "aac",
            "audio_channels": 2,
            "audio_sample_rate": 48000,
            "has_subtitle_stream": False,
        },
    }


_FIRST_OK = _result(reused=False)
_RERUN_OK = _result(job_id="job-a", reused=True)
_COUNTS_OK = {"prepare_render_job": 1, "start": 1}
_DELTA_OK: dict[str, int] = {}
_STEPS_OK = ["completed", "completed"]


def test_verdict_passes_on_observed_success() -> None:
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["passed"] is True
    assert verdict["first_not_reused"] is True
    assert verdict["rerun_reused"] is True
    assert verdict["same_job_id"] is True
    assert verdict["same_output_path"] is True
    assert verdict["same_sha256"] is True
    assert verdict["same_media_facts"] is True
    assert verdict["first_single_prepare"] is True
    assert verdict["first_single_start"] is True
    assert verdict["rerun_zero_prepare"] is True
    assert verdict["rerun_zero_start"] is True
    assert verdict["steps_completed"] is True


def test_verdict_rejects_first_reused() -> None:
    first = _result(reused=True)
    assert evidence_verdict(first, _RERUN_OK, _COUNTS_OK, _DELTA_OK, _STEPS_OK)["passed"] is False


def test_verdict_rejects_rerun_not_reused() -> None:
    rerun = _result(job_id="job-a", reused=False)
    assert evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)["passed"] is False


def test_verdict_rejects_different_job_id() -> None:
    rerun = _result(job_id="job-b", reused=True)
    verdict = evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["same_job_id"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_sentinel_job_id() -> None:
    rerun = _result(job_id="reused-existing", reused=True)
    verdict = evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["same_job_id"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_different_path() -> None:
    rerun = _result(job_id="job-a", output_path="render/other.mp4", reused=True)
    verdict = evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["same_output_path"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_different_hash() -> None:
    rerun = _result(job_id="job-a", sha="b" * 64, reused=True)
    verdict = evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["same_sha256"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_different_media_facts() -> None:
    rerun = _result(job_id="job-a", reused=True, duration=2.5)
    verdict = evidence_verdict(_FIRST_OK, rerun, _COUNTS_OK, _DELTA_OK, _STEPS_OK)
    assert verdict["same_media_facts"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_missing_first_prepare() -> None:
    counts = {"prepare_render_job": 0, "start": 1}
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, counts, _DELTA_OK, _STEPS_OK)
    assert verdict["first_single_prepare"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_missing_first_start() -> None:
    counts = {"prepare_render_job": 1, "start": 0}
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, counts, _DELTA_OK, _STEPS_OK)
    assert verdict["first_single_start"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_double_first_prepare() -> None:
    counts = {"prepare_render_job": 2, "start": 1}
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, counts, _DELTA_OK, _STEPS_OK)
    assert verdict["first_single_prepare"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_nonzero_rerun_prepare() -> None:
    delta = {"prepare_render_job": 1}
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, _COUNTS_OK, delta, _STEPS_OK)
    assert verdict["rerun_zero_prepare"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_nonzero_rerun_start() -> None:
    delta = {"start": 1}
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, _COUNTS_OK, delta, _STEPS_OK)
    assert verdict["rerun_zero_start"] is False
    assert verdict["passed"] is False


def test_verdict_rejects_incomplete_steps() -> None:
    verdict = evidence_verdict(_FIRST_OK, _RERUN_OK, _COUNTS_OK, _DELTA_OK, ["completed", "failed"])
    assert verdict["steps_completed"] is False
    assert verdict["passed"] is False
