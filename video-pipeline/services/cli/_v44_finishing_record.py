"""Operator recorders for the T13 finishing protocol.

``record-publishability`` RECORDS the operator's verdict into the existing
``PublishabilityReviewV1`` (schema ``publishability-review-v1``) under the
episode review conventions — it never decides anything itself and carries
NO autonomous publication semantics. It refuses (typed) on unfinished
episode states (no finishing report).

``record-time`` appends one ``v44-time-log-v1`` line to the episode
``time-log.jsonl``. The label is FIXED to ``bootstrap`` (PRD §19.4:
bootstrap and steady_state measurements never mix).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.cli._v44_finishing_report import (
    QUALITY_DOMAIN_NAMES,
    TimeLogLineV1,
    TimeLogPhase,
)
from services.final_review.publishability import PublishabilityReviewV1
from services.foundation_io import atomic_write, canonical_model_bytes

FINISHING_REPORT_RELATIVE: Final = ("finishing", "finishing-run.json")
PUBLISHABILITY_RELATIVE: Final = ("review", "publishability.json")
TIME_LOG_NAME: Final = "time-log.jsonl"

#: CLI verdict vocabulary -> PublishabilityReviewV1 publishable literal.
VERDICT_MAP: Final[dict[str, str]] = {
    "publishable": "as_is",
    "publishable_after_fixes": "after_small_corrections",
    "not_publishable": "not_yet",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_episode_root(episode_root: Path) -> None:
    if not episode_root.is_dir():
        raise FinishingError(
            "episode-root-not-found", f"{episode_root} is not a directory"
        )


def record_publishability(
    episode_root: Path,
    verdict: str,
    *,
    comments: str | None,
    dimension_comments: dict[str, str],
) -> PublishabilityReviewV1:
    """Write the operator verdict; refuse on an unfinished episode state."""

    _require_episode_root(episode_root)
    if verdict not in VERDICT_MAP:
        raise FinishingMalformedError(
            "verdict-invalid",
            f"verdict must be one of {sorted(VERDICT_MAP)}, got {verdict!r}",
        )
    unknown = sorted(set(dimension_comments) - set(QUALITY_DOMAIN_NAMES))
    if unknown:
        raise FinishingMalformedError(
            "dimension-unknown",
            f"dimension comments must key the seven quality domains; unknown: {unknown}",
        )
    report_path = episode_root.joinpath(*FINISHING_REPORT_RELATIVE)
    if not report_path.is_file():
        raise FinishingError(
            "finishing-report-missing",
            f"no finishing report at {report_path}; run "
            "`python -m services.cli.v44_finishing run` and watch the final "
            "preview BEFORE recording a publishability verdict",
        )
    try:
        payload = json.loads(report_path.read_bytes())
        run_id = str(payload["run_id"])
        episode_id = str(payload["episode_id"])
    except (OSError, ValueError, KeyError) as error:
        raise FinishingMalformedError(
            "finishing-report-invalid",
            f"{report_path} is not a v44-finishing-run-v1 report: {error}",
        ) from error
    overall = comments.strip() if comments and comments.strip() else None
    if dimension_comments:
        folded = " / ".join(
            f"{domain}: {text.strip()}" for domain, text in sorted(dimension_comments.items())
        )
        overall = folded if overall is None else f"{overall} / {folded}"
    review = PublishabilityReviewV1.model_validate(
        {
            "schema_version": "publishability-review-v1",
            "episode_id": episode_id,
            "run_id": run_id,
            "publishable": VERDICT_MAP[verdict],
            "overall_comment": overall,
        }
    )
    target = episode_root.joinpath(*PUBLISHABILITY_RELATIVE)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, canonical_model_bytes(review))
    return review


def record_time(episode_root: Path, phase: str, minutes: float) -> TimeLogLineV1:
    """Append one bootstrap-labeled phase measurement to time-log.jsonl."""

    _require_episode_root(episode_root)
    if minutes <= 0:
        raise FinishingMalformedError(
            "minutes-invalid", f"minutes must be > 0, got {minutes}"
        )
    line = TimeLogLineV1(
        schema_version="v44-time-log-v1",
        phase=cast("TimeLogPhase", phase),
        minutes=float(minutes),
        label="bootstrap",
        at=_now_iso(),
    )
    with (episode_root / TIME_LOG_NAME).open("ab") as stream:
        stream.write(line.model_dump_json().encode() + b"\n")
    return line


__all__ = [
    "PUBLISHABILITY_RELATIVE",
    "TIME_LOG_NAME",
    "VERDICT_MAP",
    "record_publishability",
    "record_time",
]
