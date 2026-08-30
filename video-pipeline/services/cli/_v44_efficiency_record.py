"""T8 ``record-efficiency``: carry real timing into the final proof report.

Reads ONLY existing evidence seams — the finishing run report (wall
clock), the operator ``time-log.jsonl`` (five-category AHT + direct
Resolve subset), and the V44-1 observation record (TTFRP) — and rewrites
the ``ProductProofReportV1`` efficiency block. Missing evidence stays
``null`` (never estimated); the rewritten report flips
``pass_policy.require_efficiency_timing`` on, so the gate cannot pass
while any timing entry is null.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.cli._v44_finishing_record import (
    FINISHING_REPORT_RELATIVE,
    TIME_LOG_NAME,
)
from services.cli._v44_finishing_report import (
    FinishingRunReportV1,
    TimeLogLineV1,
    summarize_time_log,
)
from services.metrics.v44_gate_state import V1ObservationRecord
from services.metrics.v44_product_proof import (
    EfficiencyMetrics,
    ProductProofReportV1,
)

OBSERVATION_NAME: Final = "observation.json"


class EfficiencyRecordError(ValueError):
    """Typed record-efficiency refusal (never a silent partial update)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _load_report(report_path: Path) -> ProductProofReportV1:
    try:
        payload: object = json.loads(report_path.read_text(encoding="utf-8"))
        return ProductProofReportV1.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        # Details suppressed: validation text echoes payload contents.
        raise EfficiencyRecordError(
            "report-invalid",
            f"{report_path} is not a readable product-proof-report-v1 payload "
            "(details suppressed)",
        ) from None


def _load_time_log(path: Path) -> tuple[TimeLogLineV1, ...]:
    if not path.is_file():
        return ()
    lines: list[TimeLogLineV1] = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(TimeLogLineV1.model_validate_json(raw))
        except ValidationError:
            # Details suppressed: validation text echoes line contents.
            raise EfficiencyRecordError(
                "time-log-invalid",
                f"{path} has a non-v44-time-log-v1 line (details suppressed)",
            ) from None
    return tuple(lines)


def _load_ttfrp(
    observation_path: Path | None, episode_root: Path, episode_id: str
) -> float | None:
    candidate = (
        observation_path if observation_path is not None else episode_root / OBSERVATION_NAME
    )
    if not candidate.is_file():
        return None
    try:
        record = V1ObservationRecord.model_validate_json(candidate.read_bytes())
    except (OSError, ValidationError):
        # Details suppressed: validation text echoes payload contents.
        raise EfficiencyRecordError(
            "observation-invalid",
            f"{candidate} is not a v44-1-observation-v1 record (details suppressed)",
        ) from None
    if str(record.episode_id) != episode_id:
        raise EfficiencyRecordError(
            "observation-episode-mismatch",
            f"the observation record at {candidate} belongs to episode "
            f"{record.episode_id!r}, not the product-proof report episode "
            f"{episode_id!r} — refusing to carry another episode's TTFRP",
        )
    return record.ttfrp_seconds


def record_efficiency(
    report_path: Path,
    episode_root: Path,
    observation_path: Path | None = None,
) -> ProductProofReportV1:
    """Return the updated report (the caller persists it atomically)."""

    report = _load_report(report_path)
    finishing_path = episode_root.joinpath(*FINISHING_REPORT_RELATIVE)
    if not finishing_path.is_file():
        raise FinishingError(
            "finishing-report-missing",
            f"no finishing report at {finishing_path}; run the finishing CLI "
            "first — the wall clock is carried from it and is never estimated",
        )
    try:
        finishing = FinishingRunReportV1.model_validate_json(finishing_path.read_bytes())
    except (OSError, ValidationError):
        # Details suppressed: validation text echoes payload contents.
        raise FinishingMalformedError(
            "finishing-report-invalid",
            f"{finishing_path} is not a v44-finishing-run-v1 report (details suppressed)",
        ) from None
    if str(finishing.episode_id) != str(report.run.episode_id):
        raise FinishingMalformedError(
            "episode-mismatch",
            f"the finishing report episode {finishing.episode_id!r} does not "
            f"match the product-proof report episode {report.run.episode_id!r}",
        )
    totals = summarize_time_log(_load_time_log(episode_root / TIME_LOG_NAME))
    previous = report.efficiency
    efficiency = EfficiencyMetrics(
        wall_clock_seconds=float(finishing.wall_clock_seconds),
        aht_minutes=totals.aht_minutes,
        direct_resolve_minutes=totals.direct_resolve_minutes,
        ttfrp_seconds=_load_ttfrp(observation_path, episode_root, str(report.run.episode_id)),
        provider_cost=previous.provider_cost if previous is not None else None,
    )
    return ProductProofReportV1(
        run=report.run,
        editorial=report.editorial,
        progressive_lift=report.progressive_lift,
        evidence_quality=report.evidence_quality,
        video_understanding=report.video_understanding,
        operator=report.operator,
        efficiency=efficiency,
        pass_policy=report.pass_policy.model_copy(
            update={"require_efficiency_timing": True}
        ),
        notes=report.notes,
        summary=report.summary,
    )


__all__ = [
    "OBSERVATION_NAME",
    "EfficiencyRecordError",
    "record_efficiency",
]
