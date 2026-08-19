"""Deterministic resolved-QC-policy authoring from a clean fixture render.

This is a PROPOSAL tool (Todo 52 acceptance authoring step): it measures the
clean fixture render with the same pinned tools the engine verifies with and
derives versioned thresholds by fixed margin rules — nothing is negotiated,
nothing is read from the future output. The engine still re-measures and may
block against the emitted policy.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Final

from services.build.render_models import RenderPresetExpectation
from services.qc.checks.audio_checks import AudioMeasure, measure_audio
from services.qc.models import (
    AudioThresholds,
    IrThresholds,
    PreviewThresholds,
    QcPolicy,
    SubtitleThresholds,
    VideoThresholds,
)
from services.qc.tools import QcTools, load_qc_tools, parse_probe_report
from services.qc.video_probe import PinnedBlackFreezeProbe

THRESHOLD_VERSION: Final = "qc-thresholds-p2a-v1"
BLACK_MIN_MS_FLOOR: Final = 1000
FREEZE_MIN_MS_FLOOR: Final = 1000
SILENCE_MAX_MS_FLOOR: Final = 1000
MARGIN_MS: Final = 500
MARGIN_MB: Final = 100
LOUDNESS_WINDOW_MLU: Final = 3000
PROBE_MS: Final = 100


def _ceil_to(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


def build_policy(render: Path, tools: QcTools | None = None) -> QcPolicy:
    resolved = tools if tools is not None else load_qc_tools()
    report, raw_streams = parse_probe_report(resolved.probe_raw(render))
    video = next(stream for stream in report.streams if stream.codec_type == "video")
    audio = next(stream for stream in report.streams if stream.codec_type == "audio")
    duration_ms = int(float(video.duration) * 1000) if video.duration else 0
    probe = PinnedBlackFreezeProbe(resolved)
    black_spans = probe.black(render, PROBE_MS, duration_ms)
    freeze_spans = probe.freeze(render, PROBE_MS, duration_ms)
    with tempfile.TemporaryDirectory(prefix="qc-policy-") as tmp_dir:
        wav = Path(tmp_dir) / "measure.wav"
        measured = measure_audio(resolved.ffmpeg, render, resolved.mono_wav, wav)
        measure = AudioMeasure(
            peak_sample=measured.peak_sample,
            peak_mb=measured.peak_mb,
            clipped_samples=measured.clipped_samples,
            loudness=measured.loudness,
            silence_spans=measured.silence_spans,
            probed_channels=audio.channels or 0,
        )
    max_black = max((span.duration_ms for span in black_spans), default=0)
    max_freeze = max((span.duration_ms for span in freeze_spans), default=0)
    max_silence = max(
        (span.end_ms - span.start_ms for span in measure.silence_spans), default=0
    )
    integrated = measure.loudness.integrated_loudness_mlufs

    def color(key: str) -> str | None:
        for stream in raw_streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                value = stream.get(key)
                return value if isinstance(value, str) else None
        return None

    has_subtitle = any(stream.codec_type == "subtitle" for stream in report.streams)
    policy = QcPolicy(
        schema_version="resolved-qc-policy-v1",
        threshold_version=THRESHOLD_VERSION,
        subtitle=SubtitleThresholds(
            min_duration_frames=15,
            max_lines=2,
            max_chars_per_line=42,
            timing_tolerance_ms=40,
            track_required=has_subtitle,
        ),
        video=VideoThresholds(
            black_min_duration_ms=max(
                BLACK_MIN_MS_FLOOR, _ceil_to(max_black + MARGIN_MS, 100)
            ),
            freeze_min_duration_ms=max(
                FREEZE_MIN_MS_FLOOR, _ceil_to(max_freeze + MARGIN_MS, 100)
            ),
            expectation=RenderPresetExpectation(
                container_format_name=report.format.format_name or "",
                video_codec=video.codec_name or "",
                width=video.width or 0,
                height=video.height or 0,
                r_frame_rate=video.r_frame_rate or "",
                pix_fmt=video.pix_fmt or "",
                audio_codec=audio.codec_name or "",
                audio_sample_rate=int(audio.sample_rate or 0),
                audio_channels=audio.channels or 0,
                color_space=color("color_space"),
                color_primaries=color("color_primaries"),
                color_transfer=color("color_transfer"),
                expected_nb_frames=video.nb_frames,
            ),
        ),
        audio=AudioThresholds(
            max_peak_mb=measure.peak_mb + MARGIN_MB,
            loudness_min_mlufs=(integrated if integrated is not None else -23000)
            - LOUDNESS_WINDOW_MLU,
            loudness_max_mlufs=(integrated if integrated is not None else -14000)
            + LOUDNESS_WINDOW_MLU,
            max_silence_ms=max(SILENCE_MAX_MS_FLOOR, _ceil_to(max_silence + MARGIN_MS, 100)),
            expected_channels=audio.channels or 0,
        ),
        preview=PreviewThresholds(
            require_binding=False,
            subtitle_expected=has_subtitle,
            max_duration_drift_ms=50,
        ),
        ir=IrThresholds(require_binding=False, expected_total_frames=None),
        required_capabilities=(),
        capability_matrix=None,
        policy_sha256="0" * 64,
    )
    return policy.model_copy(update={"policy_sha256": policy.content_hash()})


__all__ = ["THRESHOLD_VERSION", "build_policy"]
