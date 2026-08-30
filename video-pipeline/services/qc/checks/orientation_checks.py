"""Orientation QC: expected display orientation vs the actual render.

Measured root cause of the V44-2 rotated-final blocker (2026-08-29): a
live Resolve timeline can carry orientation state no render-stream metadata
exposes (API-invisible clip orientation composed with per-item transforms),
so a visibly rotated final passed every metadata check. Three machine
observations here: render rotation side data, the render's portrait/
landscape class, and an upright-frame NCC observation of IR-mapped
edit-source frames over the four quarter-turn hypotheses (fail-closed).
Frame-rotation math lives in ``orientation_frames``; no image dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from services.contracts.timeline_ir import TimelineItem0C
from services.ingest.models import VideoStreamRecord
from services.ingest.records import rotation_degrees as matrix_rotation_degrees
from services.qc.checks.orientation_frames import FRAME_SIZE, quarter_scores
from services.qc.models import QcMeasured, QcRuleId

if TYPE_CHECKING:
    from fractions import Fraction
    from pathlib import Path

    from services.contracts.timeline_ir import TimelineIrProduction
    from services.ingest.models import SourceManifest
    from services.qc.issue_factory import IssueFactory
    from services.qc.models import QcIssue
    from services.qc.tools import QcTools
    from services.resolve_bridge.fixed_presentation_models import FfprobeReport

ORIENTATION_TOOL_VERSION: Final = "orientation-ncc-v1"
MAX_SAMPLES: Final = 3
#: Calibrated on five measured representative-episode renders: noise tops
#: out at 0.199 (blurred scenes), solid truth reads 0.39-1.0; weak-truth
#: cases below the line fail closed as unverified, never pass.
MIN_BEST_NCC: Final = 0.30
MIN_MARGIN: Final = 0.05
_QUARTER_DEGREES: Final = 90
_HALF_TURN_DEGREES: Final = 180


class FrameSource(Protocol):
    def __call__(self, media: Path, at_seconds: float) -> list[float]: ...


def tools_frame_source(tools: QcTools) -> FrameSource:
    """QcTools-backed grayscale frame reader at the observation canvas."""

    def read(media: Path, at_seconds: float) -> list[float]:
        raw = tools.frame_gray(media, at_seconds, FRAME_SIZE)
        return [float(byte) for byte in raw]

    return read


@dataclass(frozen=True, slots=True)
class ExpectedOrientation:
    """Display orientation the Source Manifest records for the video."""

    rotation_degrees: int
    source_width: int
    source_height: int

    @property
    def display_portrait(self) -> bool:
        rotated_quarter = self.rotation_degrees % _HALF_TURN_DEGREES == _QUARTER_DEGREES
        source_portrait = self.source_width < self.source_height
        return source_portrait != rotated_quarter

    @property
    def quarter_turns(self) -> int:
        return (self.rotation_degrees // _QUARTER_DEGREES) % 4


def expected_orientation(manifest: SourceManifest) -> ExpectedOrientation:
    video = next(
        (s for s in manifest.streams if isinstance(s, VideoStreamRecord)), None
    )
    if video is None:
        raise ValueError("source manifest has no video stream")
    return ExpectedOrientation(
        rotation_degrees=video.rotation_degrees or 0,
        source_width=video.width,
        source_height=video.height,
    )


def _sample_placements(ir: TimelineIrProduction) -> tuple[tuple[Fraction, Fraction, int], ...]:
    """(record_seconds, source_seconds, record_frame) midpoints as exact
    rationals: record frames convert at the IR rate, source frames at the
    item's own span rate (denominators kept, e.g. 30000/1001)."""
    ordered = sorted(
        (
            item
            for track in ir.tracks
            if track.track.kind == "video"
            for item in track.items
            if isinstance(item, TimelineItem0C) and item.kind == "video"
        ),
        key=lambda item: item.record_span.end_frame - item.record_span.start_frame,
        reverse=True,
    )[:MAX_SAMPLES]
    placements: list[tuple[Fraction, Fraction, int]] = []
    for item in ordered:
        length = item.record_span.end_frame - item.record_span.start_frame
        record_mid = item.record_span.start_frame + length // 2
        offset = record_mid - item.record_span.start_frame
        source_mid = item.source.span.start_frame + offset
        record_seconds = ir.rate.duration_for(record_mid)
        source_seconds = item.source.span.rate.duration_for(source_mid)
        placements.append((record_seconds, source_seconds, record_mid))
    return tuple(placements)


@dataclass(frozen=True, slots=True)
class OrientationCheckRequest:
    render: Path
    report: FfprobeReport
    render_streams_raw: tuple[dict[str, object], ...]
    expectation: ExpectedOrientation
    ir: TimelineIrProduction | None
    edit_source: Path | None
    frame_source: FrameSource
    factory: IssueFactory


def check_orientation(request: OrientationCheckRequest) -> tuple[QcIssue, ...]:
    video = next((s for s in request.report.streams if s.codec_type == "video"), None)
    if video is None or video.width is None or video.height is None:
        return ()
    issues: list[QcIssue] = []
    issues.extend(_render_rotation_side_data_issues(request))
    issues.extend(_orientation_class_issues(request, video.width, video.height))
    issues.extend(_upright_frame_issues(request))
    return tuple(sorted(issues, key=lambda issue: issue.detail))


def _render_rotation_side_data_issues(
    request: OrientationCheckRequest,
) -> tuple[QcIssue, ...]:
    return tuple(
        request.factory.build(
            "video_orientation_mismatch",
            f"render stream carries rotation side data {degrees} degrees; the "
            "delivered final must be physically upright",
            ORIENTATION_TOOL_VERSION,
            (QcMeasured(name="render_rotation_degrees", value=str(degrees)),),
        )
        for stream in request.render_streams_raw
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
        for degrees in (matrix_rotation_degrees(stream),)
        if degrees is not None and degrees % 360 != 0
    )


def _orientation_class_issues(
    request: OrientationCheckRequest, render_width: int, render_height: int
) -> tuple[QcIssue, ...]:
    expectation = request.expectation
    render_portrait = render_width < render_height
    if render_portrait == expectation.display_portrait:
        return ()
    expected_class = "portrait" if expectation.display_portrait else "landscape"
    render_class = "portrait" if render_portrait else "landscape"
    return (
        request.factory.build(
            "video_orientation_mismatch",
            f"expected {expected_class} display "
            f"({expectation.source_width}x{expectation.source_height} at "
            f"{expectation.rotation_degrees} deg) but the render is {render_class} "
            f"({render_width}x{render_height})",
            ORIENTATION_TOOL_VERSION,
            (
                QcMeasured(name="expected_display_class", value=expected_class),
                QcMeasured(name="render_display_class", value=render_class),
            ),
        ),
    )


def _upright_frame_issues(
    request: OrientationCheckRequest,
) -> tuple[QcIssue, ...]:
    if request.ir is None or request.edit_source is None:
        return ()
    placements = _sample_placements(request.ir)
    if not placements:
        return ()
    measured: list[QcMeasured] = []
    conclusive_correct = False
    conclusive_wrong = False
    for record_seconds, source_seconds, record_frame in placements:
        render_frame = request.frame_source(request.render, float(record_seconds))
        source_image = request.frame_source(request.edit_source, float(source_seconds))
        scores = quarter_scores(render_frame, source_image)
        best = max(scores, key=lambda turn: scores[turn])
        margin = scores[best] - max(
            score for turn, score in scores.items() if turn != best
        )
        measured.extend(
            (
                QcMeasured(
                    name=f"sample_{record_frame}",
                    value=(
                        f"record@{float(record_seconds):.3f}s "
                        f"source@{float(source_seconds):.3f}s"
                    ),
                ),
                QcMeasured(
                    name=f"quarter_ncc_{record_frame}",
                    value=" ".join(
                        f"{turn}:{score:.3f}" for turn, score in scores.items()
                    ),
                ),
            )
        )
        # Rotation is a global layer property: a DOMINANT hypothesis is the
        # only signal (blurred/dark samples are noise, never a positive
        # rotation observation), one dominant upright sample proves it, and
        # any dominant rotated sample refutes it.
        if scores[best] >= MIN_BEST_NCC and margin >= MIN_MARGIN:
            if best == request.expectation.quarter_turns:
                conclusive_correct = True
            else:
                conclusive_wrong = True
                measured.append(
                    QcMeasured(name=f"observed_quarter_{record_frame}", value=str(best))
                )
    if conclusive_correct and not conclusive_wrong:
        return ()
    rule: QcRuleId = (
        "video_orientation_mismatch" if conclusive_wrong else "video_orientation_unverified"
    )
    detail = (
        f"upright-frame observation disagrees with the manifest rotation "
        f"{request.expectation.rotation_degrees} deg (expected quarter turn "
        f"{request.expectation.quarter_turns})"
        if conclusive_wrong
        else "upright-frame observation inconclusive: no dominant quarter-turn "
        "hypothesis; QC fails closed"
    )
    return (request.factory.build(rule, detail, ORIENTATION_TOOL_VERSION, tuple(measured)),)


__all__ = [
    "ORIENTATION_TOOL_VERSION",
    "ExpectedOrientation",
    "FrameSource",
    "OrientationCheckRequest",
    "check_orientation",
    "expected_orientation",
    "tools_frame_source",
]
