"""External color render-side validation (Todo 60): pinned ffmpeg, from bytes.

The deterministic external path: check the FROZEN build's filter set
honestly (``scale``/``colorspace`` are present, ``zscale`` is not — no
libzimg in this build), refuse every HDR conversion (never an unsupported
HDR path), apply the hash-verified camera LUT through ``lut3d`` plus the
equivalent bt709 conversion through ``setparams``/``scale``, and validate
the OUTPUT bytes: ffprobe color metadata against the declared targets and
measured SMPTE-bar region luma means against the DECLARED fixture
constants. API/tool success output alone never suffices.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.presentation.color_models import ColorTargets

FFMPEG_TIMEOUT_SECONDS: Final = 300
EXTRACT_TIMEOUT_SECONDS: Final = 120
COLOR_FILTER_CANDIDATES: Final = ("scale", "zscale", "colorspace")
REGION_INSET_FRACTION: Final = 4
REGION_Y: Final[tuple[int, int]] = (10, 50)
LUMA_TOLERANCE: Final = 6
SOURCE_DERIVATIVE_TOLERANCE: Final = 4

# Declared fixture expectations: the pinned ffmpeg 7.1.1 recipe
# ``smptebars + setparams=bt709 + h264_videotoolbox`` decodes to these
# top-bar luma means (limited range). Declared constants, not learned from
# any implementation output.
BAR_REGION_LUMA: Final[dict[str, int]] = {
    "gray75": 191,
    "yellow": 170,
    "cyan": 134,
    "green": 112,
    "magenta": 79,
    "red": 57,
    "blue": 22,
}

IDENTITY_CUBE_BYTES: Final = (
    b'TITLE "FVP identity v1"\n'
    b"LUT_3D_SIZE 2\n"
    b"0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"
)


class ColorRenderError(Exception):
    """The external render/validation path failed; ``code`` is the cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def _run_text(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise ColorRenderError(
            "command_timeout", f"{argv[0]} exceeded {timeout}s"
        ) from error


def _run_binary(argv: tuple[str, ...], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ColorRenderError(
            "command_timeout", f"{argv[0]} exceeded {timeout}s"
        ) from error


def available_color_filters(ffmpeg_bin: Path) -> frozenset[str]:
    """Which candidate color filters the FROZEN build actually ships."""

    result = _run_text(
        (str(ffmpeg_bin), "-nostdin", "-hide_banner", "-filters"),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ColorRenderError("filters_unavailable", "ffmpeg -filters failed")
    lines = frozenset(result.stdout.splitlines())
    return frozenset(
        name
        for name in COLOR_FILTER_CANDIDATES
        if any(line.split()[1] == name for line in lines if len(line.split()) > 1)
    )


def require_conversion_filters(
    available: frozenset[str],
    *,
    source_transfer: str = "bt709",
    target_transfer: str = "bt709",
) -> None:
    """Refuse HDR conversions outright; require a usable SDR path."""

    if source_transfer != target_transfer and (
        source_transfer in {"smpte2084", "arib-std-b67"}
        or target_transfer in {"smpte2084", "arib-std-b67"}
    ):
        raise ColorRenderError(
            "hdr_conversion_unsupported",
            f"no HDR conversion path is supported ({source_transfer} -> "
            f"{target_transfer}); this stays a typed human route",
        )
    if not (available & {"scale", "colorspace", "zscale"}):
        raise ColorRenderError(
            "conversion_unavailable",
            f"no color conversion filter in the frozen build: {sorted(available)}",
        )


def conformance_derivative_argv(
    *,
    ffmpeg_bin: Path,
    lut_path: Path,
    source: Path,
    output: Path,
    targets: ColorTargets,
) -> tuple[str, ...]:
    """The deterministic external conformance command (recorded evidence)."""

    chain = (
        f"lut3d={lut_path},"
        f"setparams=range={targets.color_range}:"
        f"color_primaries={targets.color_primaries}:"
        f"color_trc={targets.color_transfer}:"
        f"colorspace={targets.color_space},"
        f"scale=out_color_matrix={targets.color_space}"
    )
    return (
        str(ffmpeg_bin),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vf",
        chain,
        "-c:v",
        "h264_videotoolbox",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        targets.color_space,
        "-color_primaries",
        targets.color_primaries,
        "-color_trc",
        targets.color_transfer,
        str(output),
    )


@dataclass(frozen=True, slots=True)
class ConformanceDerivative:
    path: Path
    sha256: str
    argv: tuple[str, ...]
    lut_sha256: str


def render_conformance_derivative(
    *,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    lut_path: Path,
    lut_sha256: str,
    source: Path,
    output: Path,
    targets: ColorTargets,
) -> ConformanceDerivative:
    """Apply the preset + equivalent conversion externally; typed on drift."""

    del ffprobe_bin  # validation is a separate, explicit step (validate_output_color)
    try:
        observed_lut = sha256_file(lut_path)
    except OSError as error:
        raise ColorRenderError(
            "lut_hash_drift", f"preset bytes unreadable at {lut_path}: {error}"
        ) from error
    if observed_lut != lut_sha256:
        raise ColorRenderError(
            "lut_hash_drift",
            f"preset bytes drifted from the declared hash: {lut_path}",
        )
    require_conversion_filters(
        available_color_filters(ffmpeg_bin),
        source_transfer=targets.color_transfer,
        target_transfer=targets.color_transfer,
    )
    argv = conformance_derivative_argv(
        ffmpeg_bin=ffmpeg_bin,
        lut_path=lut_path,
        source=source,
        output=output,
        targets=targets,
    )
    result = _run_text(argv, timeout=FFMPEG_TIMEOUT_SECONDS)
    if result.returncode != 0 or not output.is_file():
        raise ColorRenderError(
            "derivative_render_failed", result.stderr.strip()[-500:]
        )
    return ConformanceDerivative(
        path=output,
        sha256=sha256_file(output),
        argv=argv,
        lut_sha256=lut_sha256,
    )


def validate_output_color(
    ffprobe_bin: Path, media: Path, targets: ColorTargets
) -> dict[str, str]:
    """ffprobe-declared color metadata vs the declared targets."""

    result = _run_text(
        (
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(media),
        ),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ColorRenderError("ffprobe_failed", result.stderr.strip()[-300:])
    payload: object = json.loads(result.stdout)
    streams = payload["streams"] if isinstance(payload, dict) else []
    video = next(
        (row for row in streams if isinstance(row, dict) and row.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise ColorRenderError("ffprobe_failed", f"no video stream in {media}")
    probed = {
        "color_space": str(video.get("color_space") or ""),
        "color_transfer": str(video.get("color_transfer") or ""),
        "color_primaries": str(video.get("color_primaries") or ""),
    }
    expected = {
        "color_space": targets.color_space,
        "color_transfer": targets.color_transfer,
        "color_primaries": targets.color_primaries,
    }
    if probed != expected:
        raise ColorRenderError(
            "output_metadata_mismatch",
            f"output color metadata {probed} != declared {expected}",
        )
    return probed


@dataclass(frozen=True, slots=True)
class RegionLuma:
    region_id: str
    mean: int
    expected: int
    tolerance: int
    passed: bool


def measure_region_luma(
    ffmpeg_bin: Path,
    media: Path,
    *,
    frame_index: int,
    width: int,
    height: int,
    expectations: dict[str, tuple[int, int]] | None = None,
) -> tuple[RegionLuma, ...]:
    """Measured top-bar region luma means vs DECLARED constants, from bytes."""

    expected_map = {
        name: (value, LUMA_TOLERANCE) for name, value in BAR_REGION_LUMA.items()
    }
    if expectations is not None:
        expected_map.update(expectations)
    result = _run_binary(
        (
            str(ffmpeg_bin),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or len(result.stdout) != width * height:
        raise ColorRenderError(
            "frame_extraction_failed",
            f"frame {frame_index} extraction got {len(result.stdout)} bytes: "
            f"{result.stderr[-300:]!r}",
        )
    frame = result.stdout
    bar_width = width // 7
    rows: list[RegionLuma] = []
    y0, y1 = REGION_Y
    inset = max(1, bar_width // REGION_INSET_FRACTION)
    for index, name in enumerate(BAR_REGION_LUMA):
        x0 = index * bar_width + inset
        x1 = (index + 1) * bar_width - inset
        samples = [
            frame[y * width + x]
            for y in range(y0, y1, 2)
            for x in range(max(0, x0), min(width, x1), 2)
        ]
        if not samples:
            raise ColorRenderError(
                "region_empty", f"region {name} sampled no pixels"
            )
        mean = sum(samples) // len(samples)
        expected, tolerance = expected_map[name]
        rows.append(
            RegionLuma(
                region_id=name,
                mean=mean,
                expected=expected,
                tolerance=tolerance,
                passed=abs(mean - expected) <= tolerance,
            )
        )
    failed = [row.region_id for row in rows if not row.passed]
    if failed:
        raise ColorRenderError(
            "luminance_out_of_tolerance",
            "region luma means outside the declared tolerance: "
            + ", ".join(
                f"{row.region_id}={row.mean} (want {row.expected}±{row.tolerance})"
                for row in rows
                if not row.passed
            ),
        )
    return tuple(rows)


__all__ = [
    "BAR_REGION_LUMA",
    "COLOR_FILTER_CANDIDATES",
    "IDENTITY_CUBE_BYTES",
    "ColorRenderError",
    "ConformanceDerivative",
    "RegionLuma",
    "available_color_filters",
    "conformance_derivative_argv",
    "measure_region_luma",
    "render_conformance_derivative",
    "require_conversion_filters",
    "validate_output_color",
]
