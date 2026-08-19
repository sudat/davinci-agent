"""Audio checks: pure threshold evaluation plus real measured renders."""

from __future__ import annotations

from pathlib import Path

from services.analyze.analysis_models import LoudnessSummary, SampleMsSpan
from services.qc.checks import check_audio
from services.qc.checks.audio_checks import AudioMeasure, measure_audio
from services.qc.tools import load_qc_tools
from tests.qc.support import RenderSpec, base_render, clean_policy, preset

INPUTS = ("7" * 64,)


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
