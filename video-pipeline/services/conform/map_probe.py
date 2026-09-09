"""Map build facts: pinned probing plus hash-bound file verification.

The original is re-hashed against its SourceManifest and the edit source
against its NormalizeRecord before any probing (stale artifacts refuse the
build). Video facts come from the pinned ffprobe via ``services.normalize.probe``
(metadata-first frame count with a recorded accuracy class, bounded
full-decode fallback); the original start PTS is
the ingest record's stream fact; original audio facts come from the
manifest's audio stream record; edit audio facts probe the edit source and
accept only tick-exact integral sample counts (PCM passthrough).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.conform.map_build import BuildReason, ConformMapBuildError, MapFacts
from services.conform.map_models import (
    EditAudioFacts,
    OriginalAudioFacts,
    OriginalVideoFacts,
)
from services.foundation_io import sha256_file
from services.ingest.models import AudioStreamRecord, SourceManifest, VideoStreamRecord
from services.ingest.probe import ProbeExecutionError
from services.normalize.probe import probe_media_facts, probe_media_json

if TYPE_CHECKING:
    from services.normalize.models import NormalizeRecord


def _require_hashed(path: Path, expected: str, reason: BuildReason) -> None:
    if not path.is_file():
        raise ConformMapBuildError(reason, f"file is missing: {path}")
    if sha256_file(path) != expected:
        raise ConformMapBuildError(
            reason, f"{path} no longer hashes to its recorded artifact hash"
        )


def _manifest_video(manifest: SourceManifest) -> VideoStreamRecord:
    for stream in manifest.streams:
        if isinstance(stream, VideoStreamRecord):
            return stream
    raise ConformMapBuildError("facts_mismatch", "manifest has no video stream")


def _original_audio_facts(manifest: SourceManifest) -> OriginalAudioFacts:
    for stream in manifest.streams:
        if isinstance(stream, AudioStreamRecord):
            seconds = Fraction(stream.duration_num, stream.duration_den)
            exact = seconds * stream.sample_rate
            if exact.denominator != 1:
                raise ConformMapBuildError(
                    "facts_mismatch",
                    "original audio duration is not an integral sample count",
                )
            return OriginalAudioFacts(
                sample_rate=stream.sample_rate,
                sample_count=exact.numerator,
                start_offset_samples=stream.start_offset_samples,
            )
    raise ConformMapBuildError("facts_mismatch", "manifest has no audio stream")


def _edit_audio_facts(ffprobe: Path, edit_source: Path) -> EditAudioFacts:
    payload = probe_media_json(ffprobe, edit_source)
    streams = payload.get("streams")
    audio = (
        next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ),
            None,
        )
        if isinstance(streams, list)
        else None
    )
    if not isinstance(audio, dict):
        raise ConformMapBuildError("facts_mismatch", "edit source has no audio stream")
    try:
        sample_rate = int(str(audio["sample_rate"]))
        tb_num, tb_den = str(audio["time_base"]).split("/", maxsplit=1)
        duration_ts = int(str(audio["duration_ts"]))
        start_pts = int(str(audio["start_pts"]))
    except (KeyError, ValueError) as error:
        raise ConformMapBuildError(
            "facts_mismatch", f"edit audio stream facts malformed: {error}"
        ) from error
    ticks = Fraction(duration_ts * int(tb_num), int(tb_den)) * sample_rate
    start = Fraction(start_pts * int(tb_num), int(tb_den)) * sample_rate
    if ticks.denominator != 1 or start.denominator != 1:
        raise ConformMapBuildError(
            "unsupported_stream_layout",
            "edit audio timing is not sample-aligned",
        )
    return EditAudioFacts(
        sample_rate=sample_rate,
        sample_count=ticks.numerator,
        start_offset_samples=start.numerator,
    )


def probe_map_facts(
    ffprobe: Path, *, manifest: SourceManifest, record: NormalizeRecord
) -> MapFacts:
    original = Path(manifest.file.path)
    _require_hashed(original, manifest.file.sha256, "identity_mismatch")
    edit_source = Path(record.output.path)
    _require_hashed(edit_source, record.output.sha256, "identity_mismatch")
    try:
        decoded = probe_media_facts(ffprobe, original)
    except ProbeExecutionError as error:
        raise ConformMapBuildError(
            "facts_mismatch", f"original is not decodable: {error}"
        ) from error
    ingest_video = _manifest_video(manifest)
    return MapFacts(
        video=OriginalVideoFacts(
            nb_read_frames=decoded.video.nb_read_frames,
            duration_num=decoded.video.duration_num,
            duration_den=decoded.video.duration_den,
            time_base_num=decoded.video.time_base_num,
            time_base_den=decoded.video.time_base_den,
            start_pts=ingest_video.start_pts,
        ),
        original_audio=_original_audio_facts(manifest),
        edit_audio=_edit_audio_facts(ffprobe, edit_source),
    )
