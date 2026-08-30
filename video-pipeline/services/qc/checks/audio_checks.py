"""Final-render audio checks over the Todo-34 measure stack.

The render's audio is decoded to a mono 48 kHz s16 wav by the pinned ffmpeg
and measured exactly like the analyzer stack: window RMS in mB, peak in
integer sample units plus mB, silence as half-open sample spans, and
loudness via ebur128 in mLU (honest rms_fallback labeling otherwise).
Evaluation is a pure function over those raw measurements.

Task 5 adds the rendered-media boundary for the live audio stages:
ebur128 (peak=true) loudness + true peak on the UNTOUCHED render,
ffprobe channels, mono-wav peak/floor/silence; missing or non-finite
values fail closed typed — a plan target is never a measurement.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from services.analyze.audio_constants import SILENCE_FLOOR_MB
from services.analyze.audio_measure import (
    compute_window_stats,
    detect_silence_spans,
    measure_loudness,
    peak_and_clipping,
    read_s16_mono_wav,
)
from services.analyze.audio_probe import run_bounded
from services.qc.issue_factory import IssueFactory
from services.qc.models import AudioThresholds, QcMeasured

if TYPE_CHECKING:
    from pathlib import Path

    from services.analyze.analysis_models import LoudnessSummary, SampleMsSpan
    from services.qc.models import QcIssue
    from services.qc.tools import QcTools

AUDIO_TOOL_VERSION: Final = "audio-measure-todo34-v1"
_EBUR_INTEGRATED_RE: Final = re.compile(r"(?m)^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS\s*$")
_EBUR_TRUE_PEAK_RE: Final = re.compile(r"(?m)^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dB(?:TP|FS)\s*$")


class AudioMeasurementError(Exception):
    """Typed refusal when a rendered-media measurement cannot be produced."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class AudioPolicyView(Protocol):
    """Structural policy surface the audio checks evaluate against.

    Satisfied by :class:`services.qc.models.QcPolicy` and by live policy
    views carrying only the audio thresholds and threshold version.
    """

    @property
    def threshold_version(self) -> str: ...

    @property
    def audio(self) -> AudioThresholds: ...


@dataclass(frozen=True, slots=True)
class AudioMeasure:
    """Raw measurements from the Todo-34 stack over the decoded mono wav."""

    peak_sample: int
    peak_mb: int
    clipped_samples: int
    loudness: LoudnessSummary
    silence_spans: tuple[SampleMsSpan, ...]
    probed_channels: int
    floor_mb: int = 0


def evaluate_audio_measurements(
    measure: AudioMeasure, policy: AudioPolicyView, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    factory = IssueFactory.for_policy(policy, inputs)
    issues: list[QcIssue] = []
    if measure.probed_channels != policy.audio.expected_channels:
        issues.append(
            factory.build(
                "audio_channels_mismatch",
                f"render audio carries {measure.probed_channels} channels; policy "
                f"declares {policy.audio.expected_channels}",
                AUDIO_TOOL_VERSION,
                (QcMeasured(name="channels", value=str(measure.probed_channels)),),
            )
        )
    if measure.peak_mb > policy.audio.max_peak_mb:
        issues.append(
            factory.build(
                "audio_peak_over",
                f"peak {measure.peak_mb} mB exceeds the {policy.audio.max_peak_mb} mB "
                "ceiling",
                AUDIO_TOOL_VERSION,
                (
                    QcMeasured(name="peak_sample", value=str(measure.peak_sample)),
                    QcMeasured(name="peak_mb", value=str(measure.peak_mb)),
                ),
            )
        )
    integrated = measure.loudness.integrated_loudness_mlufs
    if integrated is None:
        issues.append(
            factory.build(
                "audio_loudness_unmeasured",
                "loudness method "
                f"{measure.loudness.honest_label} provides no integrated value; "
                "the range gate cannot be verified (fail closed)",
                AUDIO_TOOL_VERSION,
                (QcMeasured(name="method", value=measure.loudness.method),),
            )
        )
    elif not policy.audio.loudness_min_mlufs <= integrated <= policy.audio.loudness_max_mlufs:
        issues.append(
            factory.build(
                "audio_loudness_out_of_range",
                f"integrated loudness {integrated} mLUFS outside the policy range "
                f"[{policy.audio.loudness_min_mlufs}, "
                f"{policy.audio.loudness_max_mlufs}] mLUFS",
                AUDIO_TOOL_VERSION,
                (QcMeasured(name="integrated_mlufs", value=str(integrated)),),
            )
        )
    issues.extend(
        factory.build(
            "audio_silence_excess",
            f"silence [{span.start_ms},{span.end_ms})ms exceeds the "
            f"{policy.audio.max_silence_ms}ms ceiling",
            AUDIO_TOOL_VERSION,
            (
                QcMeasured(name="silence_start_ms", value=str(span.start_ms)),
                QcMeasured(name="silence_end_ms", value=str(span.end_ms)),
            ),
        )
        for span in measure.silence_spans
        if span.end_ms - span.start_ms > policy.audio.max_silence_ms
    )
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


@dataclass(frozen=True, slots=True)
class RenderEbur128Levels:
    integrated_lufs: float
    true_peak_dbtp: float


def parse_ebur128_summary(stderr: str) -> RenderEbur128Levels | None:
    """Both summary values finite, or None (fail closed — never a guess)."""

    index = stderr.rfind("Summary:")
    if index < 0:
        return None
    summary = stderr[index:]
    integrated = _EBUR_INTEGRATED_RE.search(summary)
    true_peak = _EBUR_TRUE_PEAK_RE.search(summary)
    if integrated is None or true_peak is None:
        return None
    loudness, peak = float(integrated.group(1)), float(true_peak.group(1))
    if not (math.isfinite(loudness) and math.isfinite(peak)):
        return None
    return RenderEbur128Levels(integrated_lufs=loudness, true_peak_dbtp=peak)


def measure_ebur128_levels(ffmpeg: Path, render: Path) -> RenderEbur128Levels:
    result = run_bounded(
        (
            str(ffmpeg),
            "-nostdin",
            "-i",
            str(render),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ),
        300,
        "ebur128 render loudness/true-peak measurement",
    )
    levels = parse_ebur128_summary(result.stderr)
    if result.returncode != 0 or levels is None:
        raise AudioMeasurementError(
            "ebur128-levels-unavailable",
            f"ebur128 on {render.name} gave no finite summary (exit={result.returncode}) "
            f"stderr_tail={result.stderr[-240:]}",
        )
    return levels


def _audio_channels(probe_json: str, render: Path) -> int:
    try:
        streams = json.loads(probe_json).get("streams")
    except (json.JSONDecodeError, AttributeError) as error:
        raise AudioMeasurementError(
            "ffprobe-malformed", f"ffprobe payload for {render.name}: {error}"
        ) from error
    for stream in streams if isinstance(streams, list) else ():
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            channels = stream.get("channels")
            if isinstance(channels, int):
                return channels
    return 0


@dataclass(frozen=True, slots=True)
class RenderedAudioMeasure:
    integrated_loudness_lufs: float
    true_peak_dbtp: float
    channels: int
    peak_mb: int
    floor_mb: int
    max_silence_ms: int


def measure_rendered_audio(
    tools: QcTools, render: Path, work_dir: Path
) -> RenderedAudioMeasure:
    channels = _audio_channels(tools.probe_raw(render), render)
    if channels <= 0:
        raise AudioMeasurementError(
            "no-audio-stream", f"{render.name} carries no measurable audio stream"
        )
    levels = measure_ebur128_levels(tools.ffmpeg, render)
    measure = measure_audio(tools.ffmpeg, render, tools.mono_wav, work_dir / "measured.wav")
    return RenderedAudioMeasure(
        integrated_loudness_lufs=levels.integrated_lufs,
        true_peak_dbtp=levels.true_peak_dbtp,
        channels=channels,
        peak_mb=measure.peak_mb,
        floor_mb=measure.floor_mb,
        max_silence_ms=max(
            (span.end_ms - span.start_ms for span in measure.silence_spans), default=0
        ),
    )


def measure_audio(
    ffmpeg: Path,
    render: Path,
    mono_wav: Callable[[Path, Path], None],
    tmp_wav: Path,
) -> AudioMeasure:
    """Decode to mono s16 48 kHz and run the Todo-34 measurement stack.

    ``probed_channels`` stays 0 here; the caller overlays the ffprobe channel
    count from the render metadata probe."""

    mono_wav(render, tmp_wav)
    pcm = read_s16_mono_wav(tmp_wav)
    stats = compute_window_stats(pcm)
    peak, peak_mb, clipped = peak_and_clipping(pcm)
    loudness = measure_loudness(ffmpeg, tmp_wav, stats)
    silences = detect_silence_spans(stats, pcm.sample_rate)
    return AudioMeasure(
        peak_sample=peak,
        peak_mb=peak_mb,
        clipped_samples=clipped,
        loudness=loudness,
        silence_spans=silences,
        probed_channels=0,
        floor_mb=min((stat.rms_mb for stat in stats), default=SILENCE_FLOOR_MB),
    )


def check_audio(
    measure: AudioMeasure, policy: AudioPolicyView, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    return evaluate_audio_measurements(measure, policy, inputs)


__all__ = [
    "AUDIO_TOOL_VERSION",
    "AudioMeasure",
    "AudioMeasurementError",
    "AudioPolicyView",
    "RenderedAudioMeasure",
    "check_audio",
    "evaluate_audio_measurements",
    "measure_audio",
    "measure_rendered_audio",
    "parse_ebur128_summary",
]
