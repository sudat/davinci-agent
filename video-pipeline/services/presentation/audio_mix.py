"""The shared EXTERNAL MIXED DERIVATIVE renderer (Todo 59).

The pinned ffmpeg renders the deterministic program mix — edit-source bed,
BGM tone and SE at their record-frame anchors with integer-mB gains,
sample-exact fades, dialogue-triggered ducking via sidechaincompress, and
an iterative integer-mB output gain until the Todo-34 measurements land
inside the section's loudness/peak targets. The derivative is PCM WAV
bytes (byte-identical across runs on the frozen build) and is SHARED: the
preview and the final build consume the same content hash — a mismatch is
a typed parity failure, never a silent re-render.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.audio_measure import (
    compute_window_stats,
    measure_loudness,
    peak_and_clipping,
    read_s16_mono_wav,
)
from services.foundation_io import sha256_file
from services.qc.checks.audio_checks import AUDIO_TOOL_VERSION

if TYPE_CHECKING:
    from services.presentation.audio_models import (
        AudioAnchor,
        AudioProfileSection,
    )

FFMPEG_TIMEOUT_SECONDS: Final = 300
PROBE_TIMEOUT_SECONDS: Final = 120
MAX_GAIN_ITERATIONS: Final = 6
MIN_FILTER_TOKENS: Final = 2
MAX_GAIN_MB: Final = 12000
REQUIRED_FILTERS: Final[tuple[str, ...]] = (
    "amix",
    "volume",
    "afade",
    "apad",
    "atrim",
    "aformat",
    "ebur128",
)
DUCKING_FILTERS: Final[tuple[str, ...]] = ("sidechaincompress", "asplit")
DUCKING_ARGS: Final = (
    "threshold=0.02:ratio=6:attack=20:release=300:makeup=1"
)


class AudioMixError(Exception):
    """Typed external-mix failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class GainIteration:
    """One measured normalization step (integer mB / mLU / mB encodings)."""

    gain_mb: int
    integrated_mlufs: int | None
    peak_mb: int
    converged: bool


@dataclass(frozen=True, slots=True)
class MixedDerivative:
    """The rendered derivative plus its honest measurement trail."""

    path: Path
    sha256: str
    argv: tuple[str, ...]
    sample_rate_hz: int
    channels: int
    total_samples: int
    iterations: tuple[GainIteration, ...]
    integrated_mlufs: int | None
    peak_mb: int
    peak_sample: int


@dataclass(frozen=True, slots=True)
class MeasuredMix:
    integrated_mlufs: int | None
    peak_mb: int
    peak_sample: int
    channels: int


def parse_filters(stdout: str) -> frozenset[str]:
    names = set()
    for line in stdout.splitlines():
        parts = line.split()
        if (
            len(parts) >= MIN_FILTER_TOKENS
            and parts[0] and set(parts[0]) <= {"T", "S", "C", ".", "|"}
        ):
            names.add(parts[1])
    return frozenset(names)


def probe_filters(ffmpeg_bin: Path) -> frozenset[str]:
    argv = (str(ffmpeg_bin), "-nostdin", "-hide_banner", "-filters")
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=60
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise AudioMixError(
            "plugin_unavailable", f"ffmpeg -filters probe failed: {error}"
        ) from error
    if result.returncode != 0:
        raise AudioMixError("plugin_unavailable", "ffmpeg -filters probe failed")
    return parse_filters(result.stdout)


def require_mix_filters(filters: frozenset[str], *, ducking: bool) -> None:
    required: set[str] = set(REQUIRED_FILTERS)
    if ducking:
        required.update(DUCKING_FILTERS)
    missing = sorted(required - filters)
    if missing:
        raise AudioMixError(
            "plugin_unavailable",
            f"the pinned ffmpeg build lacks required mix filters: {missing}",
        )


def _volume(mb: int) -> str:
    return f"{mb / 1000:.3f}dB"


def _anchor_of(section: AudioProfileSection, role: str) -> AudioAnchor:
    for track in section.logical_tracks:
        if track.role == role and track.anchors:
            return track.anchors[0]
    raise AudioMixError(
        "render_failed", f"the section carries no {role} anchor"
    )


def _total_samples(section: AudioProfileSection) -> int:
    return _frames_to_samples(section, section.total_frames)


def _frames_to_samples(section: AudioProfileSection, frames: int) -> int:
    rate = section.frame_rate
    return frames * section.targets.sample_rate_hz * rate.den // rate.num


def _filter_graph(section: AudioProfileSection, global_gain_mb: int) -> str:
    bgm = _anchor_of(section, "music")
    se = _anchor_of(section, "sfx")
    total = _total_samples(section)
    bgm_samples = _frames_to_samples(section, bgm.record_span.length)
    fade_samples = _frames_to_samples(section, bgm.fade_in_frames)
    bgm_offset = _frames_to_samples(section, bgm.record_span.start_frame) + (
        bgm.offset_samples
    )
    se_offset = _frames_to_samples(section, se.record_span.start_frame) + (
        se.offset_samples
    )
    bgm_chain = (
        f"volume={_volume(bgm.gain_mb)},"
        f"afade=t=in:curve=tri:ss=0:ns={fade_samples},"
        f"afade=t=out:curve=tri:ss={max(0, bgm_samples - fade_samples)}:ns={fade_samples},"
        + (f"adelay=delays={bgm_offset}S:all=1," if bgm_offset else "")
        + f"apad,atrim=end_sample={total},aformat=sample_fmts=fltp"
    )
    se_chain = (
        f"volume={_volume(se.gain_mb)},"
        + (f"adelay=delays={se_offset}S:all=1," if se_offset else "")
        + f"apad,atrim=end_sample={total},aformat=sample_fmts=fltp"
    )
    bed = "[0:a]aformat=sample_fmts=fltp"
    if section.ducking.declared:
        pre = (
            f"{bed},asplit[bed_mix][bed_sc];"
            f"[1:a]{bgm_chain}[bgm_prep];"
            f"[bgm_prep][bed_sc]sidechaincompress={DUCKING_ARGS}[bgm];"
            f"[2:a]{se_chain}[se];"
        )
    else:
        pre = (
            f"{bed}[bed_mix];"
            f"[1:a]{bgm_chain}[bgm];"
            f"[2:a]{se_chain}[se];"
        )
    mix = (
        "[bed_mix][bgm][se]amix=inputs=3:duration=longest:normalize=0"
        + (f",volume={_volume(global_gain_mb)}" if global_gain_mb else "")
        + "[mix]"
    )
    return pre + mix


def _run(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise AudioMixError(
            "render_failed", f"pinned ffmpeg exceeded {timeout}s"
        ) from error
    except OSError as error:
        raise AudioMixError("render_failed", f"pinned ffmpeg failed to run: {error}") from error


def _probed_channels(ffprobe_bin: Path, wav: Path) -> int:
    argv = (
        str(ffprobe_bin),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(wav),
    )
    result = _run(argv, timeout=PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AudioMixError("render_failed", f"ffprobe failed: {result.stderr[-300:]}")
    payload: object = json.loads(result.stdout)
    streams = payload["streams"] if isinstance(payload, dict) else []
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            channels = stream.get("channels")
            return int(channels) if isinstance(channels, int) else 0
    return 0


def measure_mixed_wav(ffmpeg_bin: Path, ffprobe_bin: Path, wav: Path) -> MeasuredMix:
    """Measure the mix with the Todo-34 stack (mono decode + ebur128)."""

    with tempfile.TemporaryDirectory(prefix="audio-mix-") as tmp:
        mono = Path(tmp) / "measure-mono.wav"
        decode = _run(
            (
                str(ffmpeg_bin),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(wav),
                "-vn",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(mono),
            ),
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if decode.returncode != 0 or not mono.is_file():
            raise AudioMixError(
                "render_failed", f"mono decode failed: {decode.stderr[-300:]}"
            )
        pcm = read_s16_mono_wav(mono)
        peak, peak_mb, _clipped = peak_and_clipping(pcm)
        loudness = measure_loudness(ffmpeg_bin, mono, compute_window_stats(pcm))
    return MeasuredMix(
        integrated_mlufs=loudness.integrated_loudness_mlufs,
        peak_mb=peak_mb,
        peak_sample=peak,
        channels=_probed_channels(ffprobe_bin, wav),
    )


def _render_argv(
    ffmpeg_bin: Path,
    section: AudioProfileSection,
    bed_media: Path,
    output: Path,
    global_gain_mb: int,
) -> tuple[str, ...]:
    return (
        str(ffmpeg_bin),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(bed_media),
        "-i",
        section.assets["tone"].path,
        "-i",
        section.assets["se"].path,
        "-filter_complex",
        _filter_graph(section, global_gain_mb),
        "-map",
        "[mix]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(section.targets.sample_rate_hz),
        "-ac",
        str(section.targets.channels),
        "-map_metadata",
        "-1",
        str(output),
    )


def render_mixed_derivative(
    *,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    section: AudioProfileSection,
    bed_media: Path,
    output: Path,
) -> MixedDerivative:
    """Render the shared derivative; deterministic, normalized, typed."""

    if not bed_media.is_file():
        raise AudioMixError("bed_missing", f"bed media missing: {bed_media}")
    for kind in ("tone", "se"):
        asset = Path(section.assets[kind].path)
        if not asset.is_file() or sha256_file(asset) != section.assets[kind].sha256:
            raise AudioMixError(
                "bed_missing", f"{kind} asset bytes drifted or missing: {asset}"
            )
    require_mix_filters(
        probe_filters(ffmpeg_bin), ducking=section.ducking.declared
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    targets = section.targets
    gain = 0
    iterations: list[GainIteration] = []
    for _attempt in range(MAX_GAIN_ITERATIONS):
        argv = _render_argv(ffmpeg_bin, section, bed_media, output, gain)
        result = _run(argv, timeout=FFMPEG_TIMEOUT_SECONDS)
        if result.returncode != 0 or not output.is_file():
            raise AudioMixError(
                "render_failed", f"mix render failed: {result.stderr[-500:]}"
            )
        measured = measure_mixed_wav(ffmpeg_bin, ffprobe_bin, output)
        if measured.integrated_mlufs is None:
            raise AudioMixError(
                "loudness_unmeasured",
                f"ebur128 provided no integrated value ({AUDIO_TOOL_VERSION}); "
                "gain normalization cannot be verified and fails closed",
            )
        loud_ok = (
            abs(measured.integrated_mlufs - targets.loudness_target_mlufs)
            <= targets.loudness_tolerance_mlufs
        )
        peak_ok = measured.peak_mb <= targets.max_peak_mb
        converged = loud_ok and peak_ok
        iterations.append(
            GainIteration(
                gain_mb=gain,
                integrated_mlufs=measured.integrated_mlufs,
                peak_mb=measured.peak_mb,
                converged=converged,
            )
        )
        if converged:
            return MixedDerivative(
                path=output,
                sha256=sha256_file(output),
                argv=argv,
                sample_rate_hz=targets.sample_rate_hz,
                channels=measured.channels,
                total_samples=_total_samples(section),
                iterations=tuple(iterations),
                integrated_mlufs=measured.integrated_mlufs,
                peak_mb=measured.peak_mb,
                peak_sample=measured.peak_sample,
            )
        delta = targets.loudness_target_mlufs - measured.integrated_mlufs
        headroom = targets.max_peak_mb - measured.peak_mb
        delta = min(delta, headroom)
        gain = max(-MAX_GAIN_MB, min(MAX_GAIN_MB, gain + delta))
    raise AudioMixError(
        "normalization_failed",
        f"gain normalization did not converge in {MAX_GAIN_ITERATIONS} "
        f"iterations: {[(i.gain_mb, i.integrated_mlufs, i.peak_mb) for i in iterations]}",
    )


def verify_derivative_parity(
    derivative_sha: str, *, preview_sha: str, final_sha: str
) -> None:
    """Preview and final must consume the SAME derivative hash."""

    if preview_sha != derivative_sha or final_sha != derivative_sha:
        raise AudioMixError(
            "derivative_mismatch",
            f"preview/final derivative drift: derivative={derivative_sha} "
            f"preview={preview_sha} final={final_sha}",
        )


__all__ = [
    "DUCKING_ARGS",
    "DUCKING_FILTERS",
    "REQUIRED_FILTERS",
    "AudioMixError",
    "GainIteration",
    "MeasuredMix",
    "MixedDerivative",
    "measure_mixed_wav",
    "parse_filters",
    "probe_filters",
    "render_mixed_derivative",
    "require_mix_filters",
    "verify_derivative_parity",
]
