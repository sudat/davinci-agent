"""Preview trace checks: coverage, duration/rate, subtitle presence.

The preview trace manifest already refuses broken record coverage at the
model layer; QC adds the cross-artifact rules — the trace's read frame count
must equal the bound total, its rate must match the policy preset, its video
duration must land within the policy drift window, and its subtitle input
presence must match the committed expectation.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from services.qc.issue_factory import IssueFactory
from services.qc.models import QcMeasured

if TYPE_CHECKING:
    from services.preview.models import PreviewTraceManifest
    from services.qc.models import QcIssue, QcPolicy

PREVIEW_TOOL = "preview-trace-v1"


def _rate_fraction(text: str) -> Fraction:
    num, den = text.split("/", 1)
    return Fraction(int(num), int(den))


def check_preview(
    trace: PreviewTraceManifest | None, policy: QcPolicy, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    factory = IssueFactory.for_policy(policy, inputs)
    if trace is None:
        if policy.preview.require_binding:
            return (
                factory.build(
                    "preview_binding_missing",
                    "policy requires a preview trace binding but none was provided",
                    PREVIEW_TOOL,
                    (),
                ),
            )
        return ()
    summary = trace.ffprobe_summary
    binding = trace.timeline_binding
    measured = (
        ("nb_read_frames", str(summary.nb_read_frames)),
        ("total_record_frames", str(binding.total_record_frames)),
        ("r_frame_rate", summary.r_frame_rate),
        ("video_duration_ms", str(summary.video_duration_ms)),
    )
    issues: list[QcIssue] = []

    def measured_tuple() -> tuple[QcMeasured, ...]:
        return tuple(QcMeasured(name=name, value=value) for name, value in measured)

    expected_rate = policy.video.expectation.r_frame_rate
    if summary.r_frame_rate != expected_rate:
        issues.append(
            factory.build(
                "preview_rate_drift",
                f"preview rate {summary.r_frame_rate} != preset {expected_rate}",
                PREVIEW_TOOL,
                measured_tuple(),
            )
        )
    if summary.nb_read_frames != binding.total_record_frames:
        issues.append(
            factory.build(
                "preview_trace_coverage",
                f"preview decoded {summary.nb_read_frames} frames but the trace "
                f"binds {binding.total_record_frames}",
                PREVIEW_TOOL,
                measured_tuple(),
            )
        )
    rate = _rate_fraction(summary.r_frame_rate)
    expected_ms = Fraction(
        binding.total_record_frames * 1000 * rate.denominator, rate.numerator
    )
    drift = abs(Fraction(summary.video_duration_ms) - expected_ms)
    if drift > policy.preview.max_duration_drift_ms:
        issues.append(
            factory.build(
                "preview_duration_drift",
                f"preview video duration {summary.video_duration_ms}ms drifts "
                f"{float(drift):.3f}ms from the frame-exact {float(expected_ms):.3f}ms "
                f"(max {policy.preview.max_duration_drift_ms}ms)",
                PREVIEW_TOOL,
                measured_tuple(),
            )
        )
    has_subtitle = any(entry.kind == "subtitle" for entry in trace.inputs)
    if has_subtitle != policy.preview.subtitle_expected:
        issues.append(
            factory.build(
                "preview_subtitle_presence",
                f"preview subtitle track presence {has_subtitle} != expected "
                f"{policy.preview.subtitle_expected}",
                PREVIEW_TOOL,
                measured_tuple(),
            )
        )
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


__all__ = ["check_preview"]
