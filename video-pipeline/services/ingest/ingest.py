"""Immutable single-original registration (PRD 9: originals never mutate).

``register_one`` hashes the original before and after probing, samples a
bounded packet window per stream through the pinned ffprobe, and commits a
sealed Source Manifest atomically. Failures become structured blocked
eligibility verdicts, never crashes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.conform.coordinates import OriginalTimestamp, RationalTimeBase
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.ingest.analysis import (
    evaluate_eligibility,
    monotonicity_report,
    vfr_evidence,
)
from services.ingest.commit import ProbeOutcome, build_manifest, seal_manifest
from services.ingest.models import (
    AudioStreamRecord,
    EligibilityReason,
    RecipePointer,
    SourceManifest,
    StreamMonotonicity,
    VfrEvidence,
    VideoStreamRecord,
)
from services.ingest.probe import (
    DEFAULT_PACKET_SAMPLE_COUNT,
    PacketTimestamps,
    ProbeExecutionError,
    probe_media,
    sample_packets,
    stream_entries,
    verify_pinned_ffprobe,
)
from services.ingest.records import build_audio_record, build_video_record
from services.toolchain.normalization import NormalizationSection


class IngestError(Exception):
    """Registration could not produce a manifest at all (environment-level)."""

    LABEL = "ingest_error"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    streams: tuple[VideoStreamRecord | AudioStreamRecord, ...]
    monotonic: tuple[StreamMonotonicity, ...]
    vfr: VfrEvidence | None
    reasons: tuple[EligibilityReason, ...]


def recipe_pointer(pin_path: Path, recipe_id: str) -> RecipePointer:
    """Build the future Edit Source recipe pointer from the frozen 0B pins."""

    try:
        section = NormalizationSection.model_validate_json(pin_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise IngestError(f"invalid normalize-recipe pin: {error}") from error
    for recipe in section.recipes:
        if recipe.fixture_id == recipe_id:
            canonical = json.dumps(list(recipe.argv), ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            return RecipePointer(
                recipe_id=recipe_id, recipe_source=str(pin_path), args_sha256=digest
            )
    raise LookupError(f"recipe id is not pinned: {recipe_id}")


def _media_streams(payload: dict[str, object]) -> list[dict[str, object]]:
    return [
        stream
        for stream in stream_entries(payload)
        if stream.get("codec_type") in {"video", "audio"}
        and not (
            isinstance(disposition := stream.get("disposition"), dict)
            and disposition.get("attached_pic") == 1
        )
    ]


def _record(stream: dict[str, object]) -> VideoStreamRecord | AudioStreamRecord:
    if stream.get("codec_type") == "video":
        return build_video_record(stream)
    if stream.get("codec_type") == "audio":
        return build_audio_record(stream)
    raise ProbeExecutionError("unsupported codec_type in media stream")


def _timestamps(
    stream: dict[str, object], samples: tuple[PacketTimestamps, ...]
) -> list[OriginalTimestamp]:
    num_text, den_text = str(stream["time_base"]).split("/", maxsplit=1)
    time_base = RationalTimeBase(num=int(num_text), den=int(den_text))
    return [
        OriginalTimestamp(pts=ticks, time_base=time_base)
        for sample in samples
        if (ticks := sample.dts if sample.dts is not None else sample.pts) is not None
    ]


def _analyze(
    payload: dict[str, object], ffprobe: Path, media: Path, sample_count: int
) -> AnalysisResult:
    media_streams = _media_streams(payload)
    records = tuple(_record(stream) for stream in media_streams)
    monotonic: list[StreamMonotonicity] = []
    video_samples: tuple[PacketTimestamps, ...] = ()
    video_record: VideoStreamRecord | None = None
    for stream, record in zip(media_streams, records, strict=True):
        samples = sample_packets(ffprobe, media, record.index, sample_count)
        monotonic.append(monotonicity_report(record.index, _timestamps(stream, samples)))
        if isinstance(record, VideoStreamRecord):
            video_record = record
            video_samples = samples
    vfr = vfr_evidence(video_record, video_samples) if video_record is not None else None
    eligibility = evaluate_eligibility(
        streams_payload=payload, monotonic=tuple(monotonic), extra_reasons=()
    )
    return AnalysisResult(
        streams=records, monotonic=tuple(monotonic), vfr=vfr, reasons=eligibility.reasons
    )


def register_one(
    *,
    original: Path,
    ffprobe: Path,
    recipe: RecipePointer,
    out: Path,
    sample_count: int = DEFAULT_PACKET_SAMPLE_COUNT,
) -> SourceManifest:
    """Register one original; commit the manifest (supported or blocked)."""

    verify_pinned_ffprobe(ffprobe)
    if not original.is_file():
        raise IngestError(f"original is missing: {original}")
    hash_before = sha256_file(original)
    size = original.stat().st_size
    payload: dict[str, object] | None = None
    analysis: AnalysisResult | None = None
    corrupt: str | None = None
    try:
        payload = probe_media(ffprobe, original)
        analysis = _analyze(payload, ffprobe, original, sample_count)
    except ProbeExecutionError as error:
        corrupt = str(error)
    outcome = ProbeOutcome(
        original=original,
        size=size,
        hash_before=hash_before,
        payload=payload,
        analysis=analysis,
        corrupt=corrupt,
        changed=sha256_file(original) != hash_before,
    )
    sealed = seal_manifest(build_manifest(outcome, recipe, ffprobe))
    atomic_write(out, canonical_model_bytes(sealed))
    return sealed
