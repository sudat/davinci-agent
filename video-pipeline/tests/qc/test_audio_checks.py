"""Audio checks: pure threshold evaluation plus real measured renders."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.analyze.analysis_models import LoudnessSummary, SampleMsSpan
from services.qc.checks import check_audio
from services.qc.checks.audio_checks import (
    AudioMeasure,
    AudioMeasurementError,
    measure_audio,
    measure_rendered_audio,
    parse_ebur128_summary,
)
from services.qc.tools import load_qc_tools
from tests.qc.support import RenderSpec, base_render, clean_policy, preset

INPUTS = ("7" * 64,)

#: A measured pinned-ffmpeg ebur128 summary (Task 5 calibration run): the
#: True peak block only exists with peak=true; both values must parse.
_EBUR128_SUMMARY = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.8 LUFS
    Threshold: -24.8 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold: -34.8 LUFS
    LRA low:   -14.8 LUFS
    LRA high:  -14.8 LUFS

  True peak:
    Peak:      -13.8 dBFS
"""


def rules(issues) -> set[str]:
    return {issue.rule_id for issue in issues}


def _measure(
    *,
    peak_sample: int = 16000,
    peak_mb: int = -6000,
    loudness: LoudnessSummary | None = None,
    silence_spans: tuple[SampleMsSpan, ...] = (),
    probed_channels: int = 2,
) -> AudioMeasure:
    return AudioMeasure(
        peak_sample=peak_sample,
        peak_mb=peak_mb,
        clipped_samples=0,
        loudness=loudness
        or LoudnessSummary(
            method="ebur128",
            honest_label="itu_r_bs_1770_ebur128",
            integrated_loudness_mlufs=-20000,
            rms_mean_mb=-20000,
        ),
        silence_spans=silence_spans,
        probed_channels=probed_channels,
    )


def test_clean_measurements_pass() -> None:
    assert check_audio(_measure(), clean_policy(preset(RenderSpec())), INPUTS) == ()


def test_peak_over_blocks() -> None:
    issues = check_audio(
        _measure(peak_sample=32767, peak_mb=-3),
        clean_policy(preset(RenderSpec())),
        INPUTS,
    )
    assert rules(issues) == {"audio_peak_over"}


def test_loudness_out_of_range_blocks() -> None:
    issues = check_audio(
        _measure(
            loudness=LoudnessSummary(
                method="ebur128",
                honest_label="itu_r_bs_1770_ebur128",
                integrated_loudness_mlufs=-3000,
                rms_mean_mb=-3000,
            )
        ),
        clean_policy(preset(RenderSpec())),
        INPUTS,
    )
    assert rules(issues) == {"audio_loudness_out_of_range"}


def test_unmeasured_loudness_fails_closed() -> None:
    issues = check_audio(
        _measure(
            loudness=LoudnessSummary(
                method="rms_fallback",
                honest_label="rms_based_not_bs1770",
                integrated_loudness_mlufs=None,
                rms_mean_mb=-20000,
            )
        ),
        clean_policy(preset(RenderSpec())),
        INPUTS,
    )
    assert rules(issues) == {"audio_loudness_unmeasured"}


def test_silence_excess_blocks() -> None:
    span = SampleMsSpan(
        start_sample=0,
        end_sample=48000 * 4,
        sample_rate=48000,
        start_ms=0,
        end_ms=4000,
    )
    issues = check_audio(
        _measure(silence_spans=(span,)),
        clean_policy(preset(RenderSpec())),
        INPUTS,
    )
    assert rules(issues) == {"audio_silence_excess"}


def test_wrong_channels_block() -> None:
    issues = check_audio(
        _measure(probed_channels=1),
        clean_policy(preset(RenderSpec())),
        INPUTS,
    )
    assert rules(issues) == {"audio_channels_mismatch"}


def test_real_loud_render_measures_out_of_range(tmp_path: Path) -> None:
    tools = load_qc_tools()
    spec = RenderSpec(audio_expr="sin(2*PI*440*t)*0.9")
    render = base_render(tools.ffmpeg, tmp_path, spec, name="loud.mp4")
    wav = tmp_path / "loud-measure.wav"
    measured = measure_audio(tools.ffmpeg, render, tools.mono_wav, wav)
    measure = AudioMeasure(
        peak_sample=measured.peak_sample,
        peak_mb=measured.peak_mb,
        clipped_samples=measured.clipped_samples,
        loudness=measured.loudness,
        silence_spans=measured.silence_spans,
        probed_channels=2,
    )
    base_policy = clean_policy(preset(RenderSpec()))
    strict = base_policy.model_copy(
        update={
            "audio": base_policy.audio.model_copy(
                update={"max_peak_mb": -4000, "loudness_max_mlufs": -12000}
            )
        }
    )
    strict = strict.model_copy(update={"policy_sha256": strict.content_hash()})
    issues = check_audio(measure, strict, INPUTS)
    found = rules(issues)
    assert "audio_peak_over" in found
    assert "audio_loudness_out_of_range" in found


def test_real_mono_render_flags_channel_mismatch(tmp_path: Path) -> None:
    tools = load_qc_tools()
    spec = RenderSpec(audio_channels=1)
    render = base_render(tools.ffmpeg, tmp_path, spec, name="mono.mp4")
    wav = tmp_path / "mono-measure.wav"
    measured = measure_audio(tools.ffmpeg, render, tools.mono_wav, wav)
    measure = AudioMeasure(
        peak_sample=measured.peak_sample,
        peak_mb=measured.peak_mb,
        clipped_samples=measured.clipped_samples,
        loudness=measured.loudness,
        silence_spans=measured.silence_spans,
        probed_channels=1,
    )
    issues = check_audio(measure, clean_policy(preset(RenderSpec())), INPUTS)
    assert rules(issues) == {"audio_channels_mismatch"}


# --------------------------------------------- Task 5 measured rendered audio


def test_parse_ebur128_summary_extracts_loudness_and_true_peak() -> None:
    levels = parse_ebur128_summary(_EBUR128_SUMMARY)
    assert levels is not None
    assert levels.integrated_lufs == pytest.approx(-14.8)
    assert levels.true_peak_dbtp == pytest.approx(-13.8)


def test_parse_ebur128_summary_without_true_peak_fails_closed() -> None:
    truncated = _EBUR128_SUMMARY[: _EBUR128_SUMMARY.rfind("True peak:")]
    assert parse_ebur128_summary(truncated) is None


def test_parse_ebur128_summary_without_summary_block_is_none() -> None:
    assert parse_ebur128_summary("[Parsed_ebur128_0] t: 0.02\n") is None


def test_measure_rendered_audio_measures_a_real_render(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(
        tools.ffmpeg, tmp_path, RenderSpec(audio_expr="sin(2*PI*440*t)*0.28"), name="ok.mp4"
    )
    values = measure_rendered_audio(tools, render, tmp_path)
    assert values.channels == 2
    assert -17.0 <= values.integrated_loudness_lufs <= -13.0
    assert values.true_peak_dbtp <= 0.0
    assert values.peak_mb <= -1000
    assert values.floor_mb <= values.peak_mb
    assert values.max_silence_ms == 0


def test_measure_rendered_audio_refuses_media_without_an_audio_stream(
    tmp_path: Path,
) -> None:
    tools = load_qc_tools()
    silent = base_render(
        tools.ffmpeg,
        tmp_path,
        RenderSpec(
            video_sources=("testsrc2=s=320x180:r=30:d=4",),
        ),
        name="video-only.mp4",
    )
    # base_render always muxes an audio input; strip it for the no-audio case.
    stripped = tmp_path / "no-audio.mp4"
    tools.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(silent),
            "-map",
            "0:v",
            "-c",
            "copy",
            str(stripped),
        ),
        180,
        "strip audio stream",
    )
    with pytest.raises(AudioMeasurementError) as excinfo:
        measure_rendered_audio(tools, stripped, tmp_path)
    assert excinfo.value.code == "no-audio-stream"
