"""Typed stream-record builders parsed from raw ffprobe payloads."""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction
from typing import Final

from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    HdrSignaling,
    VideoStreamRecord,
)
from services.ingest.probe import (
    ProbeExecutionError,
    decimal_to_fraction,
    parse_rate_rational,
)

DISPLAY_MATRIX_VALUES: Final = 9
ROTATION_MATRICES: Final[dict[int, tuple[int, ...]]] = {
    0: (65536, 0, 0, 0, 65536, 0, 0, 0, 65536),
    90: (0, 65536, 0, -65536, 0, 0, 0, 0, 65536),
    180: (-65536, 0, 0, 0, -65536, 0, 0, 0, 65536),
    270: (0, -65536, 0, 65536, 0, 0, 0, 0, 65536),
}


def _field(stream: dict[str, object], key: str) -> object:
    if key not in stream:
        raise ProbeExecutionError(f"stream is missing required field {key!r}")
    return stream[key]


def _text(stream: dict[str, object], key: str) -> str:
    value = _field(stream, key)
    if not isinstance(value, str):
        raise ProbeExecutionError(f"stream field {key!r} is not a string")
    return value


def _optional_text(stream: dict[str, object], key: str) -> str | None:
    value = stream.get(key)
    return value if isinstance(value, str) else None


def _integer(stream: dict[str, object], key: str) -> int:
    value = _field(stream, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProbeExecutionError(f"stream field {key!r} is not an integer")
    return value


def _time_base(stream: dict[str, object]) -> tuple[int, int]:
    num, den = _text(stream, "time_base").split("/", maxsplit=1)
    return int(num), int(den)


def _duration_parts(stream: dict[str, object]) -> tuple[int, int]:
    tb_num, tb_den = _time_base(stream)
    ticks = _integer(stream, "duration_ts")
    seconds = Fraction(ticks * tb_num, tb_den)
    return seconds.numerator, seconds.denominator


def hdr_signaling(stream: dict[str, object]) -> HdrSignaling:
    transfer = _optional_text(stream, "color_transfer")
    dovi = False
    dovi_profile: int | None = None
    hdr10 = False
    smpte2094 = False
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("side_data_type")
            if not isinstance(kind, str):
                continue
            if kind == "DOVI configuration record":
                dovi = True
                profile = entry.get("dv_profile")
                if isinstance(profile, int) and not isinstance(profile, bool):
                    dovi_profile = profile
            if kind.startswith("HDR Mastering Display Metadata"):
                hdr10 = True
            if "SMPTE2094" in kind:
                smpte2094 = True
    return HdrSignaling(
        dolby_vision_rpu=dovi,
        dolby_vision_profile=dovi_profile,
        hdr10_mastering_display=hdr10,
        smpte2094=smpte2094,
        color_transfer=transfer,
    )


def _display_matrix_values(entry: dict[str, object]) -> list[int] | None:
    matrix_text = entry.get("displaymatrix")
    if not isinstance(matrix_text, str):
        return None
    values: list[int] = []
    for line in matrix_text.splitlines():
        if ":" not in line:
            continue
        fields = line.split(":", maxsplit=1)[1].split()
        values.extend(int(item) for item in fields if item.lstrip("-").isdigit())
    return values if len(values) >= DISPLAY_MATRIX_VALUES else None


def _degrees_for_matrix(values: Sequence[int]) -> int | None:
    for degrees, canonical in ROTATION_MATRICES.items():
        if tuple(values[:DISPLAY_MATRIX_VALUES]) == canonical:
            return degrees
    return None


def rotation_degrees(stream: dict[str, object]) -> int | None:
    """Derive rotation from the tkhd display-matrix side data (not from a
    pre-digested float angle); only exact canonical matrices map to degrees."""

    side_data = stream.get("side_data_list")
    if not isinstance(side_data, list):
        return None
    for entry in side_data:
        if not isinstance(entry, dict) or entry.get("side_data_type") != "Display Matrix":
            continue
        values = _display_matrix_values(entry)
        if values is None:
            return None
        return _degrees_for_matrix(values)
    return None


def build_video_record(stream: dict[str, object]) -> VideoStreamRecord:
    r_num, r_den = parse_rate_rational(_text(stream, "r_frame_rate"), "r_frame_rate")
    a_num, a_den = parse_rate_rational(_text(stream, "avg_frame_rate"), "avg_frame_rate")
    tb_num, tb_den = _time_base(stream)
    duration_num, duration_den = _duration_parts(stream)
    nb_frames_raw = stream.get("nb_frames")
    nb_frames = (
        int(nb_frames_raw)
        if isinstance(nb_frames_raw, str) and nb_frames_raw.strip().isdigit()
        else None
    )
    return VideoStreamRecord(
        index=_integer(stream, "index"),
        codec_type="video",
        codec_name=_text(stream, "codec_name"),
        time_base_num=tb_num,
        time_base_den=tb_den,
        start_pts=_integer(stream, "start_pts"),
        duration_num=duration_num,
        duration_den=duration_den,
        r_frame_rate_num=r_num,
        r_frame_rate_den=r_den,
        avg_frame_rate_num=a_num,
        avg_frame_rate_den=a_den,
        width=_integer(stream, "width"),
        height=_integer(stream, "height"),
        pix_fmt=_text(stream, "pix_fmt"),
        color_space=_optional_text(stream, "color_space"),
        color_transfer=_optional_text(stream, "color_transfer"),
        color_primaries=_optional_text(stream, "color_primaries"),
        color_range=_optional_text(stream, "color_range"),
        nb_frames=nb_frames,
        rotation_degrees=rotation_degrees(stream),
        hdr=hdr_signaling(stream),
    )


def build_audio_record(stream: dict[str, object]) -> AudioStreamRecord:
    tb_num, tb_den = _time_base(stream)
    duration_num, duration_den = _duration_parts(stream)
    sample_rate = int(_text(stream, "sample_rate"))
    start = Fraction(_integer(stream, "start_pts") * tb_num, tb_den)
    return AudioStreamRecord(
        index=_integer(stream, "index"),
        codec_type="audio",
        codec_name=_text(stream, "codec_name"),
        time_base_num=tb_num,
        time_base_den=tb_den,
        start_pts=_integer(stream, "start_pts"),
        duration_num=duration_num,
        duration_den=duration_den,
        sample_rate=sample_rate,
        channels=_integer(stream, "channels"),
        channel_layout=_optional_text(stream, "channel_layout"),
        start_offset_samples=math.floor(start * sample_rate),
    )


def format_duration_rational(payload: dict[str, object]) -> tuple[int, int]:
    section = payload.get("format")
    if not isinstance(section, dict):
        raise ProbeExecutionError("ffprobe payload has no format section")
    raw = section.get("duration")
    if not isinstance(raw, str):
        raise ProbeExecutionError("format duration is missing")
    seconds = decimal_to_fraction(raw)
    return seconds.numerator, seconds.denominator


def container_info(payload: dict[str, object]) -> ContainerInfo:
    section = payload.get("format")
    if not isinstance(section, dict):
        raise ProbeExecutionError("ffprobe payload has no format section")
    name = section.get("format_name")
    long_name = section.get("format_long_name")
    stream_count = section.get("nb_streams")
    if not isinstance(name, str) or isinstance(stream_count, bool) or not isinstance(
        stream_count, (int, str)
    ):
        raise ProbeExecutionError("format section is malformed")
    duration_num, duration_den = format_duration_rational(payload)
    return ContainerInfo(
        format_name=name,
        format_long_name=long_name if isinstance(long_name, str) else None,
        nb_streams=int(stream_count),
        duration_num=duration_num,
        duration_den=duration_den,
    )
