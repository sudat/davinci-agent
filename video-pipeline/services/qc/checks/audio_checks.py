"""Final-render audio checks over the Todo-34 measure stack.

The render's audio is decoded to a mono 48 kHz s16 wav by the pinned ffmpeg
and measured exactly like the analyzer stack: window RMS in mB, peak in
integer sample units plus mB, silence as half-open sample spans, and
loudness via ebur128 in mLU (honest rms_fallback labeling otherwise).
Evaluation is a pure function over those raw measurements.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.analyze.audio_measure import (
    compute_window_stats,
    detect_silence_spans,
    measure_loudness,
    peak_and_clipping,
    read_s16_mono_wav,
)
from services.qc.issue_factory import IssueFactory
from services.qc.models import QcMeasured

if TYPE_CHECKING:
    from pathlib import Path

    from services.analyze.analysis_models import LoudnessSummary, SampleMsSpan
    from services.qc.models import QcIssue, QcPolicy

AUDIO_TOOL_VERSION: Final = "audio-measure-todo34-v1"


@dataclass(frozen=True, slots=True)
class AudioMeasure:
    """Raw measurements from the Todo-34 stack over the decoded mono wav."""

    peak_sample: int
    peak_mb: int
    clipped_samples: int
    loudness: LoudnessSummary
    silence_spans: tuple[SampleMsSpan, ...]
    probed_channels: int


def evaluate_audio_measurements(
    measure: AudioMeasure, policy: QcPolicy, inputs: tuple[str, ...]
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
    )


def check_audio(
    measure: AudioMeasure, policy: QcPolicy, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    return evaluate_audio_measurements(measure, policy, inputs)


__all__ = [
    "AUDIO_TOOL_VERSION",
    "AudioMeasure",
    "check_audio",
    "evaluate_audio_measurements",
    "measure_audio",
]
