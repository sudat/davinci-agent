"""Task 7 probe evidence: reset, counting, strict result parsing, cleanup.

Every summary value must be OBSERVED: the two runner-dispatched
``render_native`` results are parsed strictly (real job id, path, sha256,
reused boolean, measured media facts) and all same-identity booleans are
computed by comparing them. Absolute repository prefixes are scrubbed from
committed evidence while relative paths stay traceable.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path

from t7_session import (
    LEDGER_DIR,
    RENDER_DIR,
    STATE_DB,
    SUMMARY,
    VIDEO_PIPELINE,
)

_EXPECTED_STEP_COUNT = 2
_SHA256_LENGTH = 64
_HEX = frozenset("0123456789abcdef")
_SENTINEL = "reused-existing"
_MEDIA_INT_KEYS = ("width", "height", "audio_channels", "audio_sample_rate")
_MEDIA_STR_KEYS = ("video_codec", "avg_frame_rate", "audio_codec")


def reset_evidence() -> None:
    """Erase BOTH ledgers and any prior outputs before execution."""

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    for entry in LEDGER_DIR.iterdir():
        if entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    for entry in RENDER_DIR.iterdir():
        if entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def count_vendor_actions(upto: int | None = None) -> dict[str, int]:
    """Vendor-level render actions from the capture log."""

    vendor_log = LEDGER_DIR / "vendor-calls.jsonl"
    if not vendor_log.is_file():
        return {}
    lines = vendor_log.read_text().splitlines()
    if upto is not None:
        lines = lines[:upto]
    counts: dict[str, int] = {}
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("tool") != "render":
            continue
        action = str(row.get("action"))
        counts[action] = counts.get(action, 0) + 1
    return counts


def count_step_actions() -> dict[str, int]:
    """Runner-recorded plan-step rows (surface-level execution evidence)."""

    ledger = LEDGER_DIR / "mcp-call-ledger.jsonl"
    if not ledger.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in ledger.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        action = str(row.get("action"))
        counts[action] = counts.get(action, 0) + 1
    return counts


def ledger_row_count() -> int:
    ledger = LEDGER_DIR / "mcp-call-ledger.jsonl"
    return len(ledger.read_text().splitlines()) if ledger.is_file() else 0


def _require_media_facts(media: object) -> dict[str, object]:
    if not isinstance(media, dict):
        raise TypeError(f"media facts missing: {media!r}")
    duration = media.get("duration_seconds")
    if not isinstance(duration, int | float) or isinstance(duration, bool) or duration <= 0:
        raise ValueError(f"invalid measured duration: {duration!r}")
    for key in _MEDIA_STR_KEYS:
        if not isinstance(media.get(key), str) or not str(media.get(key)).strip():
            raise ValueError(f"invalid measured {key}: {media.get(key)!r}")
    for key in _MEDIA_INT_KEYS:
        value = media.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"invalid measured {key}: {value!r}")
    if not isinstance(media.get("has_subtitle_stream"), bool):
        raise TypeError("invalid has_subtitle_stream")
    return media  # type: ignore[return-value]


def parse_render_result(result: object) -> dict[str, object]:
    """Strictly parse one observed render_native return value."""

    if not isinstance(result, dict):
        raise TypeError(f"render result not an object: {result!r}")
    job_id = result.get("job_id")
    if not isinstance(job_id, str) or not job_id or job_id == _SENTINEL:
        raise TypeError(f"no real job id in result: {job_id!r}")
    output_path = result.get("output_path")
    if not isinstance(output_path, str) or not output_path:
        raise ValueError(f"no output path in result: {output_path!r}")
    sha = result.get("output_sha256")
    if (
        not isinstance(sha, str)
        or len(sha) != _SHA256_LENGTH
        or not set(sha.lower()) <= _HEX
    ):
        raise ValueError(f"invalid sha256 in result: {sha!r}")
    reused = result.get("reused")
    if not isinstance(reused, bool):
        raise TypeError(f"reused is not a boolean: {reused!r}")
    return {
        "job_id": job_id,
        "output_path": output_path,
        "output_sha256": sha,
        "reused": reused,
        "media": dict(_require_media_facts(result.get("media"))),
    }


def scrub(text: str) -> str:
    prefix = f"{VIDEO_PIPELINE}/"
    return text.replace(prefix, "")


def write_summary(summary: dict[str, object]) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True)
    SUMMARY.write_text(scrub(text) + "\n")


def cleanup_artifacts() -> None:
    """Remove renders/meta/scratch/caches/state after evidence capture."""

    for p in RENDER_DIR.glob("*"):
        with contextlib.suppress(OSError):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    scratch_clip = (
        Path(str(VIDEO_PIPELINE))
        / "capabilities/v4.4/probes/task6-color-drx/.scratch/t6-source.mp4"
    )
    with contextlib.suppress(OSError):
        scratch_clip.unlink(missing_ok=True)
    probe_dir = Path(__file__).resolve().parent
    for cache in probe_dir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in probe_dir.rglob("*.pyc"):
        with contextlib.suppress(OSError):
            pyc.unlink()
    with contextlib.suppress(OSError):
        STATE_DB.unlink(missing_ok=True)


def evidence_verdict(
    first: dict[str, object],
    rerun: dict[str, object],
    first_vendor_counts: dict[str, int],
    rerun_delta: dict[str, int],
    render_step_statuses: list[str],
) -> dict[str, bool]:
    """The pure exit gate over OBSERVED values — no constants.

    Exit 0 requires: first run rendered (reused false), rerun reconciled
    (reused true), identical real job/path/sha/media between the two
    observed results, exactly one prepare+start on the first run, zero
    prepare/start on the rerun, and both runner steps completed.
    """

    rerun_job_id = rerun.get("job_id")
    checks: dict[str, bool] = {
        "first_not_reused": first.get("reused") is False,
        "rerun_reused": rerun.get("reused") is True,
        "same_job_id": (
            isinstance(rerun_job_id, str)
            and rerun_job_id != _SENTINEL
            and first.get("job_id") == rerun_job_id
        ),
        "same_output_path": (
            first.get("output_path") is not None
            and first.get("output_path") == rerun.get("output_path")
        ),
        "same_sha256": (
            first.get("output_sha256") is not None
            and first.get("output_sha256") == rerun.get("output_sha256")
        ),
        "same_media_facts": (
            isinstance(first.get("media"), dict)
            and first.get("media") == rerun.get("media")
        ),
        "first_single_prepare": first_vendor_counts.get("prepare_render_job", 0) == 1,
        "first_single_start": first_vendor_counts.get("start", 0) == 1,
        "rerun_zero_prepare": rerun_delta.get("prepare_render_job", 0) == 0,
        "rerun_zero_start": rerun_delta.get("start", 0) == 0,
        "steps_completed": (
            len(render_step_statuses) == _EXPECTED_STEP_COUNT
            and all(status == "completed" for status in render_step_statuses)
        ),
    }
    checks["passed"] = all(checks.values())
    return checks


__all__ = [
    "cleanup_artifacts",
    "count_step_actions",
    "count_vendor_actions",
    "evidence_verdict",
    "ledger_row_count",
    "parse_render_result",
    "reset_evidence",
    "write_summary",
]
