"""Event-derived human-time intervals with explicit data-quality flags.

Durations come ONLY from matched start/end marker pairs in the timing
event stream — never wall-clock guessing (PRD 22.4). Idle gaps,
self-report sourcing, log gaps, and missing timestamps are flagged per
episode; missing data becomes ``not_evaluated`` with a reason, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Final, Literal

from services.metrics.models import TIMING_PHASES
from services.metrics.report_models import DataQualityFlag, NotEvaluated, TimeSample

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle
    from services.metrics.models import EpisodeDeclaration

IDLE_GAP_THRESHOLD_S: Final = 3600
START_WITHOUT_END: Final = "phase {phase}: start marker without a matching end"
END_WITHOUT_START: Final = "phase {phase}: end marker without a matching start"
NO_TIMESTAMP: Final = "phase {phase}: interval marker carries no timestamp"
INVERTED_INTERVAL: Final = "phase {phase}: end timestamp precedes start"
SELF_REPORT: Final = "phase {phase}: interval sourced from self report"
TRAILING_START: Final = "phase {phase}: trailing start marker without an end"


@dataclass(frozen=True, slots=True)
class TimingDerivation:
    phase_samples: dict[str, list[TimeSample]]
    phase_not_evaluated: dict[str, list[NotEvaluated]]
    aht_samples: list[TimeSample]
    aht_not_evaluated: list[NotEvaluated]
    flags: list[DataQualityFlag] = field(default_factory=list)


@dataclass(slots=True)
class _PhaseAccumulator:
    total_ms: int = 0
    valid: int = 0
    reasons: list[str] = field(default_factory=list)


type FlagKind = Literal["idle_gap", "self_report_gap", "log_gap", "missing_timestamp"]


def _flag(
    episode_id: str, kind: FlagKind, detail: str, flags: list[DataQualityFlag]
) -> None:
    flags.append(DataQualityFlag(episode_id=episode_id, flag=kind, detail=detail))


def _match_intervals(
    episode_id: str,
    phase: str,
    events: list[tuple[str, int | None, str]],
    flags: list[DataQualityFlag],
) -> _PhaseAccumulator:
    accumulator = _PhaseAccumulator()
    waiting = False
    waiting_ts: int | None = None
    waiting_source = "recorded"
    for marker, timestamp, source in events:
        if marker == "start":
            if waiting:
                accumulator.reasons.append("missing end marker")
                _flag(episode_id, "log_gap", START_WITHOUT_END.format(phase=phase), flags)
            waiting = True
            waiting_ts = timestamp
            waiting_source = source
            continue
        if not waiting:
            accumulator.reasons.append("missing start marker")
            _flag(episode_id, "log_gap", END_WITHOUT_START.format(phase=phase), flags)
            continue
        waiting = False
        if waiting_ts is None or timestamp is None:
            accumulator.reasons.append("missing timestamp")
            _flag(episode_id, "missing_timestamp", NO_TIMESTAMP.format(phase=phase), flags)
        elif timestamp < waiting_ts:
            accumulator.reasons.append("inverted interval")
            _flag(episode_id, "log_gap", INVERTED_INTERVAL.format(phase=phase), flags)
        else:
            accumulator.total_ms += (timestamp - waiting_ts) * 1000
            accumulator.valid += 1
            if source == "self_report" or waiting_source == "self_report":
                _flag(episode_id, "self_report_gap", SELF_REPORT.format(phase=phase), flags)
    if waiting:
        accumulator.reasons.append("missing end marker")
        _flag(episode_id, "log_gap", TRAILING_START.format(phase=phase), flags)
    return accumulator


def _flag_idle_gaps(
    episode_id: str,
    timestamps: list[int],
    flags: list[DataQualityFlag],
) -> None:
    for previous, current in pairwise(sorted(timestamps)):
        gap = current - previous
        if gap > IDLE_GAP_THRESHOLD_S:
            flags.append(
                DataQualityFlag(
                    episode_id=episode_id,
                    flag="idle_gap",
                    detail=(
                        f"untracked gap of {gap}s between recorded events exceeds "
                        f"{IDLE_GAP_THRESHOLD_S}s (recorded, never smoothed)"
                    ),
                )
            )


def _phase_reason(
    accumulator: _PhaseAccumulator | None, *, has_events: bool
) -> str | None:
    """The single honest verdict for one (episode, phase): None = valid sample."""

    if not has_events:
        return "no timing events"
    if accumulator is not None and accumulator.reasons:
        return accumulator.reasons[0]
    return None


def _episode_samples(  # noqa: PLR0913 (per-phase verdict assembly passes the grouped bundle state)
    episode_id: str,
    grouped: dict[tuple[str, str], list[tuple[str, int | None, str]]],
    accumulators: dict[tuple[str, str], _PhaseAccumulator],
    phase_samples: dict[str, list[TimeSample]],
    phase_not_evaluated: dict[str, list[NotEvaluated]],
    *,
    genuine_ok: bool,
) -> TimeSample | None:
    """Fill per-phase verdicts for one episode; return its AHT sample or None."""

    if not genuine_ok:
        reason = NotEvaluated(
            episode_id=episode_id, reason="no genuine operator approval record"
        )
        for phase in TIMING_PHASES:
            phase_not_evaluated[phase].append(reason)
        return None
    episode_total = 0
    complete = True
    for phase in TIMING_PHASES:
        accumulator = accumulators.get((episode_id, phase))
        reason = _phase_reason(accumulator, has_events=(episode_id, phase) in grouped)
        if reason is None:
            value_ms = accumulator.total_ms if accumulator else 0
            phase_samples[phase].append(
                TimeSample(episode_id=episode_id, value_ms=value_ms)
            )
            episode_total += value_ms
        else:
            phase_not_evaluated[phase].append(
                NotEvaluated(episode_id=episode_id, reason=reason)
            )
            complete = False
    if complete:
        return TimeSample(episode_id=episode_id, value_ms=episode_total)
    return None


def derive_timing(
    bundle: EventBundle,
    denominator: tuple[EpisodeDeclaration, ...],
    genuine: frozenset[str],
) -> TimingDerivation:
    grouped: dict[tuple[str, str], list[tuple[str, int | None, str]]] = {}
    per_episode_ts: dict[str, list[int]] = {}
    for event in bundle.timing_events:
        grouped.setdefault((event.episode_id, event.phase), []).append(
            (event.marker, event.timestamp_unix, event.source)
        )
        if event.timestamp_unix is not None:
            per_episode_ts.setdefault(event.episode_id, []).append(event.timestamp_unix)

    flags: list[DataQualityFlag] = []
    declared = {episode.episode_id for episode in bundle.episodes}
    for episode_id in sorted(per_episode_ts):
        if episode_id in declared:
            _flag_idle_gaps(episode_id, per_episode_ts[episode_id], flags)
    accumulators: dict[tuple[str, str], _PhaseAccumulator] = {}
    for (episode_id, phase), events in sorted(grouped.items()):
        if episode_id in declared:
            accumulators[(episode_id, phase)] = _match_intervals(
                episode_id, phase, events, flags
            )

    phase_samples: dict[str, list[TimeSample]] = {phase: [] for phase in TIMING_PHASES}
    phase_not_evaluated: dict[str, list[NotEvaluated]] = {
        phase: [] for phase in TIMING_PHASES
    }
    aht_samples: list[TimeSample] = []
    aht_not_evaluated: list[NotEvaluated] = []
    for episode in sorted(denominator, key=lambda item: item.episode_id):
        aht = _episode_samples(
            episode.episode_id,
            grouped,
            accumulators,
            phase_samples,
            phase_not_evaluated,
            genuine_ok=episode.episode_id in genuine,
        )
        if aht is None:
            aht_not_evaluated.append(
                NotEvaluated(
                    episode_id=episode.episode_id,
                    reason=(
                        "no genuine operator approval record"
                        if episode.episode_id not in genuine
                        else "incomplete phases"
                    ),
                )
            )
        else:
            aht_samples.append(aht)
    return TimingDerivation(
        phase_samples=phase_samples,
        phase_not_evaluated=phase_not_evaluated,
        aht_samples=aht_samples,
        aht_not_evaluated=aht_not_evaluated,
        flags=flags,
    )


__all__ = [
    "IDLE_GAP_THRESHOLD_S",
    "TimingDerivation",
    "derive_timing",
]
