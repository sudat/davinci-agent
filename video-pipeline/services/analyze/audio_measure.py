"""Deterministic time-domain measurements from RAW PCM bytes.

The wav is parsed with the stdlib ``wave`` module plus a manual ``array('h')``
decode of the s16 payload (``audioop`` is deprecated on 3.12 and removed on
3.13, so nothing depends on it). All arithmetic is integer/``Decimal`` and
platform-independent:

- window RMS in milli-decibels: ``round_half_away(10000*log10(ms/2**30))``
  computed with a 60-digit ``Decimal`` context (never ``math.log10``);
- digital silence floors at the frozen ``-120000`` mB;
- peak in integer sample units plus its mB encoding;
- clipping counts samples with ``|s| >= CLIP_SAMPLE_THRESHOLD``;
- silence spans are maximal runs of windows whose RMS is below the frozen
  threshold, reported as half-open SAMPLE spans ``[i*hop, j*hop+window)``,
  kept only when at least ``MIN_SILENCE_MS`` long. Only full windows are
  measured; a partial tail shorter than the window is not.

Loudness uses the pinned ffmpeg ``ebur128`` filter when the binary provides
it (integrated loudness stored as integer milli-LUFS parsed from the exact
printed decimal); otherwise it falls back to an RMS-based value HONESTLY
labeled ``rms_based_not_bs1770`` — never a BS.1770 claim.
"""

from __future__ import annotations

import re
import wave
from array import array
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Final

from services.analyze import audio_probe
from services.analyze.analysis_models import (
    AnalyzeDecodeError,
    LoudnessSummary,
    SampleMsSpan,
    WindowStat,
)
from services.analyze.audio_constants import (
    CLIP_SAMPLE_THRESHOLD,
    DECLARED_CHANNELS,
    EBUR128_FILTER_NAME,
    FULL_SCALE_SQUARE,
    HOP_MS,
    MIN_SILENCE_MS,
    SILENCE_FLOOR_MB,
    SILENCE_RMS_THRESHOLD_MB,
    WINDOW_MS,
)

DECIMAL_PRECISION: Final = 60
EBUR128_TIMEOUT_SEC: Final = 300
SAMPLE_BYTES: Final = 2
_INTEGRATED_RE: Final = re.compile(r"(?m)^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS\s*$")
_SUMMARY_MARK: Final = "Summary:"


@dataclass(frozen=True, slots=True)
class RawPcm:
    samples: array[int]
    sample_rate: int


def read_s16_mono_wav(path: Path) -> RawPcm:
    """Parse a 16-bit mono PCM wav into raw sample values."""

    try:
        with wave.open(str(path)) as stream:
            channels = stream.getnchannels()
            width = stream.getsampwidth()
            rate = stream.getframerate()
            frames = stream.getnframes()
            raw = stream.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        raise AnalyzeDecodeError(f"wav cannot be parsed as pcm_s16le: {error}") from error
    if channels != DECLARED_CHANNELS or width != SAMPLE_BYTES:
        raise AnalyzeDecodeError(
            f"expected mono 16-bit pcm, got channels={channels} width={width}"
        )
    if len(raw) % SAMPLE_BYTES != 0:
        raise AnalyzeDecodeError("wav payload has a truncated (odd-byte) s16 sample")
    samples: array[int] = array("h")
    samples.frombytes(raw)
    return RawPcm(samples=samples, sample_rate=rate)


def milli_belles(mean_square: int) -> int:
    """Integer milli-dB of RMS amplitude vs full scale, exact via Decimal."""

    if mean_square <= 0:
        return SILENCE_FLOOR_MB
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        ratio = Decimal(mean_square) / Decimal(FULL_SCALE_SQUARE)
        shifted = ratio.log10() * 10000
        return int(shifted.to_integral_value(rounding=ROUND_HALF_UP))


def _window_grid(sample_rate: int) -> tuple[int, int]:
    window = sample_rate * WINDOW_MS // 1000
    hop = sample_rate * HOP_MS // 1000
    if window <= 0 or hop <= 0 or window < hop:
        raise ValueError(f"invalid frozen window grid at {sample_rate} Hz")
    return window, hop


def compute_window_stats(pcm: RawPcm) -> tuple[WindowStat, ...]:
    window, hop = _window_grid(pcm.sample_rate)
    samples = pcm.samples
    total = len(samples)
    stats: list[WindowStat] = []
    start = 0
    while start + window <= total:
        accumulator = 0
        for value in samples[start : start + window]:
            accumulator += value * value
        mean_square = accumulator // window
        stats.append(
            WindowStat(
                start_sample=start,
                end_sample=start + window,
                mean_square=mean_square,
                rms_mb=milli_belles(mean_square),
            )
        )
        start += hop
    return tuple(stats)


def detect_silence_spans(
    stats: tuple[WindowStat, ...], sample_rate: int
) -> tuple[SampleMsSpan, ...]:
    min_samples = sample_rate * MIN_SILENCE_MS // 1000
    spans: list[tuple[int, int]] = []
    run_start: int | None = None
    run_end: int | None = None
    for stat in stats:
        if stat.rms_mb < SILENCE_RMS_THRESHOLD_MB:
            if run_start is None:
                run_start = stat.start_sample
            run_end = stat.end_sample
        elif run_start is not None and run_end is not None:
            spans.append((run_start, run_end))
            run_start = None
            run_end = None
    if run_start is not None and run_end is not None:
        spans.append((run_start, run_end))
    return tuple(
        _span(pair[0], pair[1], sample_rate)
        for pair in spans
        if pair[1] - pair[0] >= min_samples
    )


def _span(start: int, end: int, sample_rate: int) -> SampleMsSpan:
    return SampleMsSpan(
        start_sample=start,
        end_sample=end,
        sample_rate=sample_rate,
        start_ms=start * 1000 // sample_rate,
        end_ms=end * 1000 // sample_rate,
    )


def peak_and_clipping(pcm: RawPcm) -> tuple[int, int, int]:
    """Return (peak |sample|, peak mB, clipping sample count)."""

    peak = 0
    clipped = 0
    for value in pcm.samples:
        magnitude = -value if value < 0 else value
        peak = max(peak, magnitude)
        if magnitude >= CLIP_SAMPLE_THRESHOLD:
            clipped += 1
    return peak, milli_belles(peak * peak), clipped


def _ebur128_available(ffmpeg: Path) -> bool:
    result = audio_probe.run_bounded(
        (str(ffmpeg), "-nostdin", "-hide_banner", "-filters"), 60, "ffmpeg filters probe"
    )
    if result.returncode != 0:
        return False
    return any(
        f" {EBUR128_FILTER_NAME} " in line or line.rstrip().endswith(f" {EBUR128_FILTER_NAME}")
        for line in result.stdout.splitlines()
    )


def _parse_integrated_mlufs(stderr: str) -> int | None:
    # ffmpeg logs running per-frame "I:" values; only the Summary block is final
    summary_index = stderr.rfind(_SUMMARY_MARK)
    if summary_index < 0:
        return None
    matches = _INTEGRATED_RE.findall(stderr[summary_index:])
    if not matches:
        return None
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return int((Decimal(matches[-1]) * 1000).to_integral_value())


def measure_loudness(ffmpeg: Path, wav: Path, stats: tuple[WindowStat, ...]) -> LoudnessSummary:
    total = sum(stat.mean_square for stat in stats)
    count = len(stats)
    rms_mean_mb = milli_belles(total // count) if count else SILENCE_FLOOR_MB
    if _ebur128_available(ffmpeg):
        result = audio_probe.run_bounded(
            (
                str(ffmpeg),
                "-nostdin",
                "-i",
                str(wav),
                "-filter_complex",
                EBUR128_FILTER_NAME,
                "-f",
                "null",
                "-",
            ),
            EBUR128_TIMEOUT_SEC,
            "ebur128 loudness measurement",
        )
        integrated = _parse_integrated_mlufs(result.stderr)
        if result.returncode == 0 and integrated is not None:
            return LoudnessSummary(
                method="ebur128",
                honest_label="itu_r_bs_1770_ebur128",
                integrated_loudness_mlufs=integrated,
                rms_mean_mb=rms_mean_mb,
            )
    return LoudnessSummary(
        method="rms_fallback",
        honest_label="rms_based_not_bs1770",
        integrated_loudness_mlufs=None,
        rms_mean_mb=rms_mean_mb,
    )
