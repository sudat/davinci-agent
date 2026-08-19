"""Subtitle checks: the Todo-44 rule set on the render's demuxed track.

The render's first subtitle stream is demuxed to SRT by the pinned ffmpeg
and every cue is re-validated against the versioned thresholds — minimum
display duration (frames at the preset rate), max lines, max chars per
line, safe-area text capacity, and cue non-overlap — then compared against
the committed cues derived from the bound IR subtitle track (timing drift
within tolerance; exact text).
"""

from __future__ import annotations

from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from services.preview.srt import SubtitleCue, parse_srt
from services.qc.issue_factory import IssueFactory
from services.qc.models import QcMeasured

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.qc.models import QcIssue, QcPolicy

SUBTITLE_TOOL_VERSION: Final = "subtitle-todo44-v1"


def _rate_ms(policy: QcPolicy) -> Fraction:
    num, den = policy.video.expectation.r_frame_rate.split("/", 1)
    return Fraction(1000 * int(den), int(num))


def evaluate_cues(
    cues: tuple[SubtitleCue, ...], policy: QcPolicy, factory: IssueFactory
) -> list[QcIssue]:
    issues: list[QcIssue] = []
    min_ms = policy.subtitle.min_duration_frames * _rate_ms(policy)
    for index, cue in enumerate(cues):
        measured = (
            QcMeasured(name="start_ms", value=str(cue.start_ms)),
            QcMeasured(name="end_ms", value=str(cue.end_ms)),
        )
        duration = cue.end_ms - cue.start_ms
        if Fraction(duration) < min_ms:
            issues.append(
                factory.build(
                    "subtitle_min_duration",
                    f"cue displays {duration}ms below the minimum "
                    f"{policy.subtitle.min_duration_frames} frames",
                    SUBTITLE_TOOL_VERSION,
                    measured,
                    item_id=f"cue-{index:03d}",
                )
            )
        lines = cue.text.split("\n")
        if len(lines) > policy.subtitle.max_lines:
            issues.append(
                factory.build(
                    "subtitle_max_lines",
                    f"cue wraps into {len(lines)} lines over "
                    f"{policy.subtitle.max_lines}",
                    SUBTITLE_TOOL_VERSION,
                    (QcMeasured(name="lines", value=str(len(lines))),),
                    item_id=f"cue-{index:03d}",
                )
            )
        overlong = [len(line) for line in lines if len(line) > policy.subtitle.max_chars_per_line]
        if overlong:
            issues.append(
                factory.build(
                    "subtitle_max_chars",
                    f"line exceeds {policy.subtitle.max_chars_per_line} chars: "
                    f"{overlong}",
                    SUBTITLE_TOOL_VERSION,
                    (QcMeasured(name="chars", value=str(overlong)),),
                    item_id=f"cue-{index:03d}",
                )
            )
        capacity = policy.subtitle.max_lines * policy.subtitle.max_chars_per_line
        volume = sum(len(line) for line in lines)
        if volume > capacity:
            issues.append(
                factory.build(
                    "subtitle_safe_area",
                    f"cue text volume {volume} chars exceeds the safe-area capacity "
                    f"{capacity}",
                    SUBTITLE_TOOL_VERSION,
                    (QcMeasured(name="text_chars", value=str(volume)),),
                    item_id=f"cue-{index:03d}",
                )
            )
    ordered = sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.text))
    issues.extend(
        factory.build(
            "subtitle_cue_overlap",
            f"cue starting {current.start_ms}ms overlaps the cue ending "
            f"{previous.end_ms}ms",
            SUBTITLE_TOOL_VERSION,
            (
                QcMeasured(name="overlap_start_ms", value=str(current.start_ms)),
                QcMeasured(name="previous_end_ms", value=str(previous.end_ms)),
            ),
        )
        for previous, current in pairwise(ordered)
        if current.start_ms < previous.end_ms
    )
    return issues


def drift_issues(
    cues: tuple[SubtitleCue, ...],
    committed: tuple[SubtitleCue, ...],
    policy: QcPolicy,
    factory: IssueFactory,
) -> list[QcIssue]:
    tolerance = policy.subtitle.timing_tolerance_ms
    by_text: dict[str, list[SubtitleCue]] = {}
    for cue in committed:
        by_text.setdefault(cue.text, []).append(cue)
    typed: list[QcIssue] = []
    for cue in cues:
        measured = (
            QcMeasured(name="start_ms", value=str(cue.start_ms)),
            QcMeasured(name="end_ms", value=str(cue.end_ms)),
        )
        candidates = [
            candidate
            for candidate in by_text.get(cue.text, ())
            if abs(candidate.start_ms - cue.start_ms) <= 1000 + tolerance
        ]
        if not candidates:
            typed.append(
                factory.build(
                    "subtitle_text_drift",
                    "demuxed cue text has no committed counterpart",
                    SUBTITLE_TOOL_VERSION,
                    measured,
                )
            )
            continue
        nearest = min(candidates, key=lambda candidate: abs(candidate.start_ms - cue.start_ms))
        if (
            abs(nearest.start_ms - cue.start_ms) > tolerance
            or abs(nearest.end_ms - cue.end_ms) > tolerance
        ):
            typed.append(
                factory.build(
                    "subtitle_timing_drift",
                    f"cue drifts beyond the {tolerance}ms tolerance vs the committed "
                    f"[{nearest.start_ms},{nearest.end_ms})ms",
                    SUBTITLE_TOOL_VERSION,
                    measured,
                )
            )
    return typed


def check_subtitles(
    demuxed_srt: bytes | None,
    committed: tuple[SubtitleCue, ...],
    policy: QcPolicy,
    inputs: tuple[str, ...],
) -> tuple[QcIssue, ...]:
    factory = IssueFactory.for_policy(policy, inputs)
    required = policy.subtitle.track_required or bool(committed)
    if demuxed_srt is None:
        if required:
            return (
                factory.build(
                    "subtitle_track_missing",
                    "policy or committed cues require a subtitle track but the render "
                    "carries none",
                    SUBTITLE_TOOL_VERSION,
                    (QcMeasured(name="track", value="missing"),),
                ),
            )
        return ()
    cues = parse_srt(demuxed_srt)
    issues = evaluate_cues(cues, policy, factory)
    if committed:
        issues.extend(drift_issues(cues, committed, policy, factory))
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


def committed_cues_from_ir(
    ir: TimelineIrProduction | None,
) -> tuple[SubtitleCue, ...]:
    """Cues derived from the IR subtitle track with the package ms rounding."""

    if ir is None:
        return ()
    cues: list[SubtitleCue] = []
    for track in ir.tracks:
        if track.track.kind != "subtitle":
            continue
        for item in track.items:
            if item.kind != "subtitle_cue":
                continue
            cues.append(
                SubtitleCue(
                    start_ms=_span_ms(
                        item.record_span.start_frame, ir.rate.num, ir.rate.den
                    ),
                    end_ms=_span_ms(item.record_span.end_frame, ir.rate.num, ir.rate.den),
                    text=item.text,
                )
            )
    return tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.text)))


def _span_ms(frames: int, num: int, den: int) -> int:
    return (frames * 1000 * den + num // 2) // num


__all__ = [
    "SUBTITLE_TOOL_VERSION",
    "check_subtitles",
    "committed_cues_from_ir",
    "drift_issues",
    "evaluate_cues",
]
