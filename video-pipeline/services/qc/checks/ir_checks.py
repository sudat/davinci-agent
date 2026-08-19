"""Deterministic Timeline-IR conformance checks (span/track/link/totals).

Reuses the Todo-44 rule ids where they overlap (subtitle rules, track kinds,
Resolve-field ban) and adds the structural rules: every track tiles
``[0, total)`` with no gaps or overlaps, linked A/V items share one record
span per link id, and the video total equals the audio total (and the policy
expectation when declared).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.compile.ir_qc import run_ir_qc
from services.compile.subtitle_policy import SubtitleQcPolicy
from services.contracts.timeline_ir import TimelineItem0C
from services.qc.issue_factory import IssueFactory

if TYPE_CHECKING:
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import (
        TimelineIrProduction,
        TimelineTrackProduction,
    )
    from services.qc.models import QcIssue, QcPolicy

QC_TOOL = "ir-structural-v1"
QC_FIXED_STYLE = "qc-fixed-style"


def _subtitle_policy(policy: QcPolicy) -> SubtitleQcPolicy:
    return SubtitleQcPolicy(
        policy_id=f"qc-{policy.threshold_version}",
        min_duration_frames=policy.subtitle.min_duration_frames,
        max_lines=policy.subtitle.max_lines,
        max_chars_per_line=policy.subtitle.max_chars_per_line,
        declared_style_refs=(QC_FIXED_STYLE,),
        default_style_ref=QC_FIXED_STYLE,
    )


def _coverage_issues(
    track: TimelineTrackProduction, factory: IssueFactory
) -> tuple[list[QcIssue], int]:
    issues: list[QcIssue] = []
    cursor = 0
    for item in sorted(track.items, key=lambda entry: entry.record_span.start_frame):
        span = item.record_span
        if span.end_frame <= span.start_frame:
            continue
        if span.start_frame > cursor:
            issues.append(
                factory.build(
                    "ir_span_gap",
                    f"track {track.track.kind}/{track.track.index} has no coverage "
                    f"for record frames [{cursor},{span.start_frame})",
                    QC_TOOL,
                    (),
                    item_id=item.item_id,
                    record_span=span,
                )
            )
        elif span.start_frame < cursor:
            issues.append(
                factory.build(
                    "ir_span_overlap",
                    f"track {track.track.kind}/{track.track.index} overlaps at record "
                    f"frame {span.start_frame} (previous end {cursor})",
                    QC_TOOL,
                    (),
                    item_id=item.item_id,
                    record_span=span,
                )
            )
        cursor = max(cursor, span.end_frame)
    return issues, cursor


def _link_issues(ir: TimelineIrProduction, factory: IssueFactory) -> list[QcIssue]:
    by_kind: dict[str, dict[str, list[tuple[str, RecordFrameSpan]]]] = {
        "video": {},
        "audio": {},
    }
    for track in ir.tracks:
        for item in track.items:
            if not isinstance(item, TimelineItem0C) or item.av_link_id is None:
                continue
            if item.kind in ("video", "audio"):
                bucket = by_kind[item.kind].setdefault(item.av_link_id, [])
                bucket.append((item.item_id, item.record_span))
    issues: list[QcIssue] = []
    for link_id in sorted(set(by_kind["video"]) | set(by_kind["audio"])):
        video = by_kind["video"].get(link_id, [])
        audio = by_kind["audio"].get(link_id, [])
        if len(video) != 1 or len(audio) != 1:
            issues.append(
                factory.build(
                    "ir_link_mismatch",
                    f"av link {link_id} pairs {len(video)} video with {len(audio)} "
                    "audio items; exactly 1:1 required",
                    QC_TOOL,
                    (),
                )
            )
            continue
        if video[0][1] != audio[0][1]:
            issues.append(
                factory.build(
                    "ir_link_mismatch",
                    f"av link {link_id} record spans differ between its video and "
                    "audio items",
                    QC_TOOL,
                    (),
                )
            )
    return issues


def check_ir(
    ir: TimelineIrProduction | None, policy: QcPolicy, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    factory = IssueFactory.for_policy(policy, inputs)
    if ir is None:
        if policy.ir.require_binding:
            return (
                factory.build(
                    "ir_binding_missing",
                    "policy requires an IR binding but none was provided",
                    QC_TOOL,
                    (),
                ),
            )
        return ()
    issues: list[QcIssue] = [
        factory.build(
            violation.rule_id,
            violation.detail,
            QC_TOOL,
            (),
            item_id=violation.item_id,
        )
        for violation in run_ir_qc(ir, _subtitle_policy(policy))
    ]
    totals: dict[str, int] = {}
    for track in ir.tracks:
        coverage, cursor = _coverage_issues(track, factory)
        issues.extend(coverage)
        totals[track.track.kind] = cursor
    video_total = totals.get("video")
    audio_total = totals.get("audio")
    if video_total is not None and audio_total is not None and video_total != audio_total:
        issues.append(
            factory.build(
                "ir_totals_mismatch",
                f"video track totals {video_total} frames but audio totals "
                f"{audio_total}; linked A/V must agree",
                QC_TOOL,
                (),
            )
        )
    expected = policy.ir.expected_total_frames
    if expected is not None and video_total is not None and video_total != expected:
        issues.append(
            factory.build(
                "ir_totals_mismatch",
                f"video track totals {video_total} frames; policy declares {expected}",
                QC_TOOL,
                (),
            )
        )
    issues.extend(_link_issues(ir, factory))
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


__all__ = ["check_ir"]
