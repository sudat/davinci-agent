"""Build a ConformMap from ingest facts and the committed NormalizeRecord.

All conversions go through the frozen ``services.conform`` functions; no
independent frame arithmetic. The source span uses the todo-22 decode-level
basis, so recomputed drop/dup accounting must equal the NormalizeRecord
exactly or the build refuses; inexpressible audio layouts are refused, not
invented.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from services.conform.convert import frame_conversion_accounting, pts_floor_for_video_frame
from services.conform.coordinates import RationalTimeBase
from services.conform.map_models import (
    AudioAffineMap,
    ConformMap,
    EditSourceIdentity,
    MapFacts,
    MapNormalization,
    OriginalIdentity,
    OriginalVideoFacts,
    VideoMapRow,
    VideoTable,
    compute_table_sha256,
    seal_conform_map,
)
from services.conform.rate_model import assign_ticks, tick_to_source_index
from services.contracts.primitives import (
    ArtifactRef,
    Producer,
    RationalFrameRate,
    SourceFrameSpan,
)
from services.ingest.models import SourceManifest, VideoStreamRecord

if TYPE_CHECKING:
    from services.normalize.models import NormalizeRecord

BuildReason = Literal[
    "identity_mismatch",
    "facts_mismatch",
    "record_accounting_mismatch",
    "unsupported_stream_layout",
    "invalid_content_offset",
]


class ConformMapBuildError(ValueError):
    label = "map_build_failed"

    def __init__(self, reason_code: BuildReason, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _video_stream(manifest: SourceManifest) -> VideoStreamRecord:
    for stream in manifest.streams:
        if isinstance(stream, VideoStreamRecord):
            return stream
    raise ConformMapBuildError("facts_mismatch", "manifest has no video stream")


def _require_consistent_identity(manifest: SourceManifest, record: NormalizeRecord) -> None:
    if (
        record.source.sha256 != manifest.file.sha256
        or record.inputs[0].artifact_id != manifest.artifact_id
        or record.inputs[0].sha256 != manifest.content_hash
    ):
        raise ConformMapBuildError(
            "identity_mismatch", "normalize record is not bound to this source manifest"
        )


def _require_facts_match_manifest(
    facts: MapFacts, manifest: SourceManifest
) -> RationalTimeBase:
    video = _video_stream(manifest)
    time_base = RationalTimeBase(
        num=facts.video.time_base_num, den=facts.video.time_base_den
    )
    if (
        facts.video.time_base_num != video.time_base_num
        or facts.video.time_base_den != video.time_base_den
        or Fraction(facts.video.duration_num, facts.video.duration_den)
        != Fraction(video.duration_num, video.duration_den)
    ):
        raise ConformMapBuildError(
            "facts_mismatch",
            "probed video facts disagree with the source manifest stream record",
        )
    if facts.video.start_pts != 0:
        raise ConformMapBuildError(
            "unsupported_stream_layout",
            f"nonzero video start_pts {facts.video.start_pts} not frozen in map v1",
        )
    return time_base


def _source_span(facts: OriginalVideoFacts) -> SourceFrameSpan:
    if facts.nb_read_frames <= 0 or facts.duration_num <= 0:
        raise ConformMapBuildError(
            "facts_mismatch", "decoded video span must be non-empty"
        )
    rate = Fraction(facts.nb_read_frames * facts.duration_den, facts.duration_num)
    return SourceFrameSpan(
        start_frame=0,
        end_frame=facts.nb_read_frames,
        rate=RationalFrameRate(num=rate.numerator, den=rate.denominator),
    )


def _video_rows(
    span: SourceFrameSpan,
    target: RationalFrameRate,
    time_base: RationalTimeBase,
    output_frames: int,
) -> tuple[VideoMapRow, ...]:
    pts_seconds = [span.rate.duration_for(offset) for offset in range(span.length)]
    assigned = assign_ticks(pts_seconds, target.as_fraction)
    mapping = tick_to_source_index(assigned, output_frames)
    rows: list[VideoMapRow] = []
    for edit_frame, source_index in enumerate(mapping):
        rows.append(
            VideoMapRow(
                edit_frame=edit_frame,
                source_frame=span.start_frame + source_index,
                original_pts=pts_floor_for_video_frame(
                    source_index, span.rate, time_base
                ),
                duplicate=edit_frame > 0 and mapping[edit_frame - 1] == source_index,
            )
        )
    return tuple(rows)


def _audio_map(facts: MapFacts, record: NormalizeRecord) -> AudioAffineMap:
    if facts.original_audio.sample_rate != facts.edit_audio.sample_rate:
        raise ConformMapBuildError(
            "unsupported_stream_layout",
            "audio rate conversion requires an explicit pair table",
        )
    if facts.original_audio.sample_rate != record.target.sample_rate:
        raise ConformMapBuildError(
            "facts_mismatch", "edit audio rate differs from the record target rate"
        )
    return AudioAffineMap(
        type="sample_affine",
        original_sample_rate=facts.original_audio.sample_rate,
        edit_sample_rate=facts.edit_audio.sample_rate,
        origin_original_sample=facts.original_audio.start_offset_samples,
        origin_edit_sample=facts.edit_audio.start_offset_samples,
        rate_equal=True,
        original_sample_count=facts.original_audio.sample_count,
        edit_sample_count=facts.edit_audio.sample_count,
    )


def build_conform_map(
    source_manifest: SourceManifest,
    normalize_record: NormalizeRecord,
    facts: MapFacts,
    *,
    audio_content_offset_samples: int = 0,
) -> ConformMap:
    _require_consistent_identity(source_manifest, normalize_record)
    time_base = _require_facts_match_manifest(facts, source_manifest)
    span = _source_span(facts.video)
    target = normalize_record.target.frame_rate
    accounting = frame_conversion_accounting(span, target)
    expected = normalize_record.drop_dup.expected
    if (
        accounting.output_frames != expected.output_frames
        or accounting.dropped_source_frames != tuple(expected.dropped)
        or accounting.duplicated_source_frames != tuple(expected.duplicated)
    ):
        raise ConformMapBuildError(
            "record_accounting_mismatch",
            "recomputed drop/dup accounting disagrees with the normalize record",
        )
    video = _video_stream(source_manifest)
    audio_map = _audio_map(facts, normalize_record)
    if not 0 <= audio_content_offset_samples < max(facts.original_audio.sample_count, 1):
        raise ConformMapBuildError(
            "invalid_content_offset",
            f"audio content offset {audio_content_offset_samples} outside the "
            "original audio span",
        )
    video_table = VideoTable(
        type="pts_to_frame_table",
        source_frame_count=span.length,
        source_rate=span.rate,
        output_frames=accounting.output_frames,
        rows=_video_rows(span, target, time_base, accounting.output_frames),
    )
    unsealed = ConformMap(
        schema_version="conform-map-v1",
        artifact_type="conform-map",
        artifact_id=f"conform-map-{normalize_record.output.sha256[:16]}",
        content_hash="0" * 64,
        producer=Producer(name="services.conform", version="1"),
        inputs=(
            ArtifactRef(
                artifact_id=source_manifest.artifact_id,
                sha256=source_manifest.content_hash,
            ),
            ArtifactRef(
                artifact_id=normalize_record.artifact_id,
                sha256=normalize_record.content_hash,
            ),
        ),
        map_version="1",
        original=OriginalIdentity(
            source_id=source_manifest.artifact_id,
            path=source_manifest.file.path,
            sha256=source_manifest.file.sha256,
        ),
        edit_source=EditSourceIdentity(
            path=normalize_record.output.path,
            sha256=normalize_record.output.sha256,
            normalize_record_artifact_id=normalize_record.artifact_id,
            normalize_record_content_hash=normalize_record.content_hash,
        ),
        normalization=MapNormalization(
            recipe_id=source_manifest.edit_source_recipe.recipe_id,
            target_frame_rate=target,
            sample_rate=normalize_record.target.sample_rate,
            rotation_policy=normalize_record.policy.rotation,
            rotation_degrees=video.rotation_degrees,
            audio_content_offset_samples=audio_content_offset_samples,
            output_frames=accounting.output_frames,
            dropped_source_frames=accounting.dropped_source_frames,
            duplicated_source_frames=accounting.duplicated_source_frames,
            basis="conform-frame-conversion-accounting-v1",
        ),
        video_table=video_table,
        audio_map=audio_map,
        table_sha256=compute_table_sha256(video_table, audio_map),
        replay="identical-bytes-for-identical-inputs",
    )
    return seal_conform_map(unsealed)
