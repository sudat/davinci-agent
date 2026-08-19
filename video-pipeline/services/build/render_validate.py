"""Independent Final Render output inspection and preset/fingerprint binding.

Nothing here trusts the render API: the completed file is re-inspected from
bytes — existence, size, sha256, a pinned-ffprobe raw JSON stream table, and
a full-decode pass through the pinned ffmpeg — and EVERY stream field is
compared against the frozen preset expectations. A validated record
structurally REQUIRES the raw ffprobe JSON and a clean decode (exit 0), so
an API-success-only result cannot be represented. The record binds the
output to the conformed timeline fingerprint (Todo 50), the preset hash,
the render job id, and the output sha256; a wrong fingerprint, preset, or
file is a typed permanent failure with the raw evidence attached.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import ValidationError

from services.build.render_models import (
    DecodeEvidence,
    RenderColorTable,
    RenderJobFailure,
    RenderJobRecord,
    RenderMismatch,
    RenderOutputBinding,
    RenderPresetExpectation,
    ValidatedRenderOutput,
    preset_sha256,
)
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)
from services.resolve_bridge.fixed_presentation_tools import DecodeOutcome, MediaTools

if TYPE_CHECKING:
    from services.contracts.primitives import Sha256
    from services.resolve_adapter.models import RenderJobSpec

INSPECTION_TIMEOUT_SECONDS: Final = 600


class RenderInspectionTools(Protocol):
    def probe_raw(self, path: Path) -> str: ...

    def sha256(self, path: Path) -> str: ...

    def decode(self, path: Path) -> DecodeOutcome: ...


class PinnedRenderInspectionTools:
    """Inspection over the pinned ffmpeg/ffprobe; raw output IS the evidence."""

    def __init__(self, *, ffmpeg_bin: Path, ffprobe_bin: Path) -> None:
        self._tools = MediaTools(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
        self._ffprobe_bin = ffprobe_bin

    def probe_raw(self, path: Path) -> str:
        argv = (
            str(self._ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        )
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=INSPECTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RenderJobFailure(
                "ffprobe-failed", f"ffprobe exceeded {INSPECTION_TIMEOUT_SECONDS}s on {path}"
            ) from error
        if result.returncode != 0 or not result.stdout.strip():
            raise RenderJobFailure(
                "ffprobe-failed",
                f"ffprobe failed on {path}: exit={result.returncode} {result.stderr.strip()}",
            )
        return result.stdout

    def sha256(self, path: Path) -> str:
        return self._tools.sha256(path)

    def decode(self, path: Path) -> DecodeOutcome:
        return self._tools.decode(path)


def _parse_report(raw: str) -> tuple[FfprobeReport, RenderColorTable]:
    try:
        payload = json.loads(raw)
        streams_raw = payload["streams"]
        streams = tuple(
            FfprobeStream.model_validate(
                {key: stream[key] for key in FfprobeStream.model_fields if key in stream}
            )
            for stream in streams_raw
        )
        fmt_payload = payload["format"]
        fmt = FfprobeFormat.model_validate(
            {key: fmt_payload[key] for key in FfprobeFormat.model_fields if key in fmt_payload}
        )
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValidationError,
    ) as error:
        raise RenderJobFailure("ffprobe-failed", f"ffprobe payload malformed: {error}") from error
    return FfprobeReport(streams=streams, format=fmt), _color_table(streams_raw)


def _color_table(streams: list[object]) -> RenderColorTable:
    """Color metadata is parsed from the raw video-stream dict, never hashed models."""

    def field(name: str) -> str | None:
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                value = stream.get(name)
                return value if isinstance(value, str) else None
        return None

    return RenderColorTable(
        color_space=field("color_space"),
        color_primaries=field("color_primaries"),
        color_transfer=field("color_transfer"),
        color_range=field("color_range"),
    )


def _mismatches(
    format_name: str | None,
    video: FfprobeStream,
    audio: FfprobeStream,
    color: RenderColorTable,
    expectation: RenderPresetExpectation,
) -> tuple[RenderMismatch, ...]:
    found: list[RenderMismatch] = []

    def flag(field: str, observed: object, expected: object) -> None:
        if observed != expected:
            found.append(
                RenderMismatch(field=field, observed=str(observed), expected=str(expected))
            )

    flag("format_name", format_name, expectation.container_format_name)
    flag("video_codec", video.codec_name, expectation.video_codec)
    flag("width", video.width, expectation.width)
    flag("height", video.height, expectation.height)
    flag("r_frame_rate", video.r_frame_rate, expectation.r_frame_rate)
    flag("pix_fmt", video.pix_fmt, expectation.pix_fmt)
    if expectation.expected_nb_frames is not None:
        flag("nb_frames", video.nb_frames, expectation.expected_nb_frames)
    flag("audio_codec", audio.codec_name, expectation.audio_codec)
    flag("audio_sample_rate", audio.sample_rate, str(expectation.audio_sample_rate))
    flag("audio_channels", audio.channels, expectation.audio_channels)
    if expectation.color_space is not None:
        flag("color_space", color.color_space, expectation.color_space)
    if expectation.color_primaries is not None:
        flag("color_primaries", color.color_primaries, expectation.color_primaries)
    if expectation.color_transfer is not None:
        flag("color_transfer", color.color_transfer, expectation.color_transfer)
    return tuple(found)


def validate_render_output(
    record: RenderJobRecord,
    *,
    preset: RenderJobSpec,
    expectation: RenderPresetExpectation,
    authoritative_fingerprint: Sha256,
    tools: RenderInspectionTools,
) -> ValidatedRenderOutput:
    computed_preset = preset_sha256(preset)
    if record.preset_sha256 != computed_preset:
        raise RenderJobFailure(
            "preset-mismatch",
            f"render record preset hash {record.preset_sha256} != preset hash {computed_preset}",
        )
    if record.timeline_conformance_fingerprint != authoritative_fingerprint:
        raise RenderJobFailure(
            "fingerprint-mismatch",
            f"render record binds timeline fingerprint {record.timeline_conformance_fingerprint} "
            f"but the conformed timeline is {authoritative_fingerprint}",
        )
    output = Path(record.output_path)
    if not output.is_file():
        raise RenderJobFailure("output-missing", f"render output missing: {output}")
    size = output.stat().st_size
    if size <= 0:
        raise RenderJobFailure("output-empty", f"render output is empty: {output}")
    sha = tools.sha256(output)
    raw = tools.probe_raw(output)
    report, color = _parse_report(raw)
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    if video is None or audio is None:
        missing = "video" if video is None else "audio"
        raise RenderJobFailure("metadata-mismatch", f"render output has no {missing} stream")
    mismatches = _mismatches(report.format.format_name, video, audio, color, expectation)
    if mismatches:
        detail = "; ".join(
            f"{m.field}: {m.observed!r} != {m.expected!r}" for m in mismatches
        )
        raise RenderJobFailure("metadata-mismatch", detail)
    decode = tools.decode(output)
    if decode.exit_code != 0:
        raise RenderJobFailure(
            "decode-failed",
            f"full decode exited {decode.exit_code}: {decode.stderr_tail[-300:]}",
        )
    try:
        return ValidatedRenderOutput(
            render_job_id=record.job_id,
            output_path=str(output),
            output_size_bytes=size,
            output_sha256=sha,
            ffprobe_json=raw,
            video=video,
            audio=audio,
            color=color,
            decode=DecodeEvidence(
                argv=decode.argv, exit_code=0, stderr_tail=decode.stderr_tail
            ),
            binding=RenderOutputBinding(
                timeline_conformance_fingerprint=record.timeline_conformance_fingerprint,
                preset_sha256=record.preset_sha256,
                render_job_id=record.job_id,
                output_sha256=sha,
            ),
        )
    except ValidationError as error:  # defense: an unevidenced record is never returned
        raise RenderJobFailure("validation-evidence-incomplete", str(error)) from error


__all__ = [
    "INSPECTION_TIMEOUT_SECONDS",
    "PinnedRenderInspectionTools",
    "RenderInspectionTools",
    "validate_render_output",
]
