"""Pinned ffprobe execution and exact rational parsing.

The ffprobe binary is hash-verified against the frozen Phase-0B toolchain
lock before every use; float fields from ffprobe are never stored — integer
ticks plus time bases are the canonical representation.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Final, cast

from services.foundation_io import sha256_file

FROZEN_FFPROBE_SHA256: Final = "44d20924faa535d1ac616bac5345e1c2a50a9ea697ea722033cbd664a5b62df5"
PROBE_TIMEOUT_SECONDS: Final = 120
DEFAULT_PACKET_SAMPLE_COUNT: Final = 200


class ProbeError(ValueError):
    LABEL = "probe_error"


class ProbeToolDriftError(ProbeError):
    """The ffprobe binary no longer matches the frozen toolchain lock."""

    LABEL = "ffprobe_hash_drift"


class ProbeExecutionError(ProbeError):
    """ffprobe failed, timed out, or reported decode-level errors."""

    LABEL = "corrupt_decode"


@dataclass(frozen=True, slots=True)
class PacketTimestamps:
    pts: int | None
    dts: int | None


def verify_pinned_ffprobe(ffprobe: Path) -> None:
    if not ffprobe.is_absolute():
        raise ProbeToolDriftError(f"ffprobe path must be absolute: {ffprobe}")
    if not ffprobe.is_file() or sha256_file(ffprobe) != FROZEN_FFPROBE_SHA256:
        raise ProbeToolDriftError(f"ffprobe binary hash drift: {ffprobe}")


def parse_int_rational(value: str) -> tuple[int, int]:
    """Parse ``"num/den"`` or ``"num"`` into a reduced (num, den) pair."""

    try:
        text = value.strip()
        if "/" in text:
            num_text, den_text = text.split("/", maxsplit=1)
            num, den = int(num_text), int(den_text)
        else:
            num, den = int(text), 1
    except ValueError as error:
        raise ProbeExecutionError(f"rational field {value!r} is malformed") from error
    if num < 0 or den <= 0:
        raise ProbeExecutionError(f"rational field {value!r} is out of range")
    divisor = Fraction(num, den)
    return divisor.numerator, divisor.denominator


def parse_rate_rational(value: str, what: str) -> tuple[int, int]:
    text = value.strip()
    num_text, _, den_text = text.partition("/")
    try:
        num = int(num_text)
        den = int(den_text) if den_text else 1
    except ValueError as error:
        raise ProbeExecutionError(f"{what} {value!r} is malformed") from error
    if num == 0:
        raise ProbeExecutionError(f"{what} is unknown (0/0) where a real rate is required")
    if num < 0 or den <= 0:
        raise ProbeExecutionError(f"{what} {value!r} is out of range")
    rate = Fraction(num, den)
    return rate.numerator, rate.denominator


def decimal_to_fraction(value: str) -> Fraction:
    """Parse ffprobe's fixed-point decimal string exactly (no float math)."""

    return Fraction(Decimal(value.strip()))


def _run_json(argv: tuple[str, ...]) -> dict[str, object]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError("ffprobe exceeded the bounded timeout") from error
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.strip() or "ffprobe exited without success"
        raise ProbeExecutionError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeExecutionError("ffprobe produced invalid JSON") from error
    if not isinstance(payload, dict):
        raise ProbeExecutionError("ffprobe payload is not an object")
    return payload


def probe_media(ffprobe: Path, media: Path) -> dict[str, object]:
    return _run_json(
        (
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(media),
        )
    )


def sample_packets(
    ffprobe: Path, media: Path, stream_index: int, count: int = DEFAULT_PACKET_SAMPLE_COUNT
) -> tuple[PacketTimestamps, ...]:
    payload = _run_json(
        (
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-select_streams",
            f"{stream_index}",
            "-read_intervals",
            f"0%+#{count}",
            "-show_packets",
            "-show_entries",
            "packet=pts,dts",
            str(media),
        )
    )
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ProbeExecutionError("ffprobe packet payload is malformed")
    samples: list[PacketTimestamps] = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise ProbeExecutionError("ffprobe packet entry is malformed")
        samples.append(
            PacketTimestamps(
                pts=_packet_int(packet, "pts"), dts=_packet_int(packet, "dts")
            )
        )
    return tuple(samples)


def _packet_int(packet: dict[str, object], key: str) -> int | None:
    value = packet.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def stream_entries(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    streams = payload.get("streams")
    if not isinstance(streams, list) or not all(isinstance(item, dict) for item in streams):
        raise ProbeExecutionError("ffprobe stream list is malformed")
    return cast("tuple[dict[str, object], ...]", tuple(streams))
