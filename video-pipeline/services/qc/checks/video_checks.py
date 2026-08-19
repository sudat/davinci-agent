"""Final-render video checks: decode, metadata-vs-preset, black and freeze.

Decode evidence is the pinned ffmpeg exit code, never a render-API claim.
Metadata is compared field-by-field against the policy preset expectation
(Todo-51 semantics). Black/freeze spans arrive from the pinned probe
(``services.qc.video_probe``) and become issues when they exceed the
versioned thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from services.qc.issue_factory import IssueFactory
from services.qc.models import QcMeasured, QcRuleId

if TYPE_CHECKING:
    from pathlib import Path

    from services.qc.models import QcIssue, QcPolicy
    from services.qc.video_probe import SpanMs
    from services.resolve_bridge.fixed_presentation_models import FfprobeReport

VIDEO_TOOL_VERSION = "video-pinned-ffmpeg-v1"


class SpanSource(Protocol):
    def __call__(
        self, render: Path, min_duration_ms: int, video_duration_ms: int
    ) -> tuple[SpanMs, ...]: ...


@dataclass(frozen=True, slots=True)
class VideoCheckRequest:
    render: Path
    decode_outcome: tuple[int, str]
    report: FfprobeReport
    policy: QcPolicy
    inputs: tuple[str, ...]
    ffmpeg_sha256: str
    black_source: SpanSource
    freeze_source: SpanSource


@dataclass(frozen=True, slots=True)
class _SpanRule:
    rule_id: QcRuleId
    label: str
    spans: tuple[SpanMs, ...]
    threshold_ms: int


def metadata_issues(
    report: FfprobeReport, policy: QcPolicy, factory: IssueFactory
) -> tuple[tuple[QcIssue, ...], int]:
    """Compare every preset field; returns the issues plus video duration ms."""

    expectation = policy.video.expectation
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    if video is None or audio is None:
        missing = "video" if video is None else "audio"
        return (
            (
                factory.build(
                    "video_metadata_mismatch",
                    f"render output has no {missing} stream",
                    VIDEO_TOOL_VERSION,
                    (QcMeasured(name="streams", value=missing),),
                ),
            ),
            0,
        )
    observed: tuple[tuple[str, object, object], ...] = (
        ("format_name", report.format.format_name, expectation.container_format_name),
        ("video_codec", video.codec_name, expectation.video_codec),
        ("width", video.width, expectation.width),
        ("height", video.height, expectation.height),
        ("r_frame_rate", video.r_frame_rate, expectation.r_frame_rate),
        ("pix_fmt", video.pix_fmt, expectation.pix_fmt),
        ("audio_codec", audio.codec_name, expectation.audio_codec),
        ("audio_sample_rate", audio.sample_rate, str(expectation.audio_sample_rate)),
        ("audio_channels", audio.channels, expectation.audio_channels),
    )
    measured = tuple(QcMeasured(name=name, value=str(value)) for name, value, _ in observed)
    issues = [
        factory.build(
            "video_metadata_mismatch",
            f"{name}: {value!r} != preset {expected!r}",
            VIDEO_TOOL_VERSION,
            measured,
        )
        for name, value, expected in observed
        if value != expected
    ]
    if expectation.expected_nb_frames is not None and video.nb_frames is not None:
        frame_pair = (QcMeasured(name="nb_frames", value=str(video.nb_frames)),)
        if video.nb_frames != expectation.expected_nb_frames:
            issues.append(
                factory.build(
                    "video_metadata_mismatch",
                    f"nb_frames: {video.nb_frames!r} != preset "
                    f"{expectation.expected_nb_frames!r}",
                    VIDEO_TOOL_VERSION,
                    frame_pair,
                )
            )
    duration_ms = int(float(video.duration) * 1000) if video.duration else 0
    return tuple(sorted(issues, key=lambda i: i.detail)), duration_ms


def _span_issues(rule: _SpanRule, factory: IssueFactory, tool: str) -> list[QcIssue]:
    threshold = (QcMeasured(name="threshold_ms", value=str(rule.threshold_ms)),)
    return [
        factory.build(
            rule.rule_id,
            f"{rule.label} span [{span.start_ms},{span.end_ms})ms exceeds the "
            f"{rule.threshold_ms}ms threshold",
            tool,
            (*threshold,
             QcMeasured(name=f"{rule.label}_start_ms", value=str(span.start_ms)),
             QcMeasured(name=f"{rule.label}_end_ms", value=str(span.end_ms))),
        )
        for span in rule.spans
    ]


def check_video(request: VideoCheckRequest) -> tuple[QcIssue, ...]:
    factory = IssueFactory.for_policy(request.policy, request.inputs)
    exit_code, stderr_tail = request.decode_outcome
    if exit_code != 0:
        return (
            factory.build(
                "video_decode_failed",
                f"full decode exited {exit_code}: {stderr_tail}",
                request.ffmpeg_sha256,
                (
                    QcMeasured(name="decode_exit", value=str(exit_code)),
                    QcMeasured(name="stderr_tail", value=stderr_tail[-120:] or "empty"),
                ),
            ),
        )
    issues: list[QcIssue] = []
    meta_issues, duration_ms = metadata_issues(request.report, request.policy, factory)
    issues.extend(meta_issues)
    rules = (
        _SpanRule(
            rule_id="video_black_span",
            label="black",
            spans=request.black_source(
                request.render, request.policy.video.black_min_duration_ms, duration_ms
            ),
            threshold_ms=request.policy.video.black_min_duration_ms,
        ),
        _SpanRule(
            rule_id="video_freeze_span",
            label="freeze",
            spans=request.freeze_source(
                request.render, request.policy.video.freeze_min_duration_ms, duration_ms
            ),
            threshold_ms=request.policy.video.freeze_min_duration_ms,
        ),
    )
    for rule in rules:
        issues.extend(_span_issues(rule, factory, request.ffmpeg_sha256))
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


__all__ = ["VIDEO_TOOL_VERSION", "VideoCheckRequest", "check_video", "metadata_issues"]
