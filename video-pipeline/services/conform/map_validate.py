"""Semantic ConformMap validation: recompute everything from the table bytes.

Coverage, monotonicity, dup reporting, and ``table_sha256`` are recomputed
from the serialized tables (never trusted from build flags), so a tampered
map — even consistently re-hashed — is rejected. When the originating
manifest/record are supplied, identity and accounting are cross-checked
against them (wrong-source and stale records refuse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.conform.convert import pts_floor_for_video_frame
from services.conform.coordinates import PtsSpan, require_monotonic_pts
from services.conform.errors import NonMonotonicPtsError
from services.conform.map_models import (
    AudioAffineMap,
    AudioTableMap,
    ConformMap,
    compute_table_sha256,
)
from services.conform.map_query import original_span_to_edit_span

if TYPE_CHECKING:
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord

ValidationReason = Literal[
    "identity_mismatch",
    "accounting_mismatch",
    "inputs_mismatch",
    "coverage_gap",
    "coverage_overlap",
    "unreported_duplicate",
    "non_monotonic_table",
    "inconsistent_table",
    "audio_map_invalid",
    "table_hash_mismatch",
]


class MapValidationError(ValueError):
    label = "map_invalid"

    def __init__(self, reason_code: ValidationReason, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _validate_table_hash(conform_map: ConformMap) -> None:
    recomputed = compute_table_sha256(conform_map.video_table, conform_map.audio_map)
    if recomputed != conform_map.table_sha256:
        raise MapValidationError(
            "table_hash_mismatch",
            f"table_sha256 {conform_map.table_sha256} != recomputed {recomputed}",
        )


def _validate_row_shape(conform_map: ConformMap) -> None:
    table = conform_map.video_table
    rows = table.rows
    if len(rows) != table.output_frames:
        raise MapValidationError(
            "coverage_gap",
            f"table holds {len(rows)} rows for {table.output_frames} output frames",
        )
    for index, row in enumerate(rows):
        if row.edit_frame != index:
            raise MapValidationError(
                "coverage_gap",
                f"row {index} claims edit_frame {row.edit_frame}; rows must cover "
                "[0, output_frames) exactly once",
            )
    time_bases = {(row.original_pts.time_base.num, row.original_pts.time_base.den) for row in rows}
    if len(time_bases) > 1:
        raise MapValidationError(
            "inconsistent_table", "rows mix more than one original time base"
        )
    try:
        require_monotonic_pts([row.original_pts for row in rows])
    except NonMonotonicPtsError as error:
        raise MapValidationError("non_monotonic_table", str(error)) from error


def _validate_source_partition(conform_map: ConformMap) -> None:
    table = conform_map.video_table
    dropped = set(conform_map.normalization.dropped_source_frames)
    duplicated = set(conform_map.normalization.duplicated_source_frames)
    shown = {row.source_frame for row in table.rows}
    all_frames = set(range(table.source_frame_count))
    if shown & dropped or shown - all_frames or dropped - all_frames:
        raise MapValidationError(
            "coverage_overlap",
            "shown and dropped source frames must partition the decoded span",
        )
    if (shown | dropped) != all_frames:
        missing = sorted(all_frames - shown - dropped)
        raise MapValidationError(
            "coverage_gap", f"source frames missing from map: {missing[:8]}"
        )
    _validate_duplicate_flags(conform_map, duplicated)


def _validate_duplicate_flags(conform_map: ConformMap, duplicated: set[int]) -> None:
    rows = conform_map.video_table.rows
    flagged: set[int] = set()
    for index in range(1, len(rows)):
        previous, current = rows[index - 1], rows[index]
        if current.source_frame < previous.source_frame:
            raise MapValidationError(
                "non_monotonic_table",
                f"source frame {current.source_frame} at row {index} precedes "
                f"{previous.source_frame}",
            )
        shares = current.source_frame == previous.source_frame
        if shares and not current.duplicate:
            raise MapValidationError(
                "unreported_duplicate",
                f"row {index} repeats source frame {current.source_frame} "
                "without a duplicate flag",
            )
        if shares and current.original_pts != previous.original_pts:
            raise MapValidationError(
                "inconsistent_table",
                f"rows {index - 1}/{index} share a source frame but not its PTS",
            )
        if current.duplicate and not shares:
            raise MapValidationError(
                "unreported_duplicate",
                f"row {index} flags a duplicate its previous row does not back"
            )
        if shares:
            flagged.add(current.source_frame)
    if flagged != duplicated:
        raise MapValidationError(
            "unreported_duplicate",
            f"flagged duplicates {sorted(flagged)} != reported {sorted(duplicated)}",
        )


def _validate_audio_map(conform_map: ConformMap) -> None:
    audio = conform_map.audio_map
    if isinstance(audio, AudioAffineMap):
        if audio.rate_equal != (audio.original_sample_rate == audio.edit_sample_rate):
            raise MapValidationError(
                "audio_map_invalid", "rate_equal contradicts the stored rates"
            )
        if (
            audio.origin_original_sample > audio.original_sample_count
            or audio.origin_edit_sample > audio.edit_sample_count
        ):
            raise MapValidationError(
                "audio_map_invalid", "affine origins outside their sample spans"
            )
        return
    if not isinstance(audio, AudioTableMap):
        raise MapValidationError("audio_map_invalid", "unknown audio map variant")
    for index in range(1, len(audio.pairs)):
        if (
            audio.pairs[index].original_sample <= audio.pairs[index - 1].original_sample
            or audio.pairs[index].edit_sample <= audio.pairs[index - 1].edit_sample
        ):
            raise MapValidationError(
                "audio_map_invalid", f"sample pair {index} is not strictly increasing"
            )
    for pair in audio.pairs:
        if (
            pair.original_sample >= audio.original_sample_count
            or pair.edit_sample >= audio.edit_sample_count
        ):
            raise MapValidationError("audio_map_invalid", "sample pair out of bounds")


def _validate_against_sources(
    conform_map: ConformMap,
    manifest: SourceManifest,
    record: NormalizeRecord,
) -> None:
    if (
        conform_map.original.source_id != manifest.artifact_id
        or conform_map.original.sha256 != manifest.file.sha256
        or conform_map.original.path != manifest.file.path
        or record.source.sha256 != manifest.file.sha256
        or record.inputs[0].artifact_id != manifest.artifact_id
    ):
        raise MapValidationError(
            "identity_mismatch",
            "map identity does not bind this source manifest",
        )
    if (
        conform_map.edit_source.sha256 != record.output.sha256
        or conform_map.edit_source.path != record.output.path
        or conform_map.edit_source.normalize_record_artifact_id != record.artifact_id
        or conform_map.edit_source.normalize_record_content_hash != record.content_hash
    ):
        raise MapValidationError("identity_mismatch", "map does not bind this record")
    expected = record.drop_dup.expected
    if (
        conform_map.normalization.output_frames != expected.output_frames
        or conform_map.normalization.dropped_source_frames != tuple(expected.dropped)
        or conform_map.normalization.duplicated_source_frames != tuple(expected.duplicated)
        or conform_map.normalization.target_frame_rate != record.target.frame_rate
        or conform_map.normalization.sample_rate != record.target.sample_rate
        or conform_map.normalization.recipe_id != manifest.edit_source_recipe.recipe_id
    ):
        raise MapValidationError(
            "accounting_mismatch",
            "normalization block disagrees with the normalize record",
        )
    required = {
        (manifest.artifact_id, manifest.content_hash),
        (record.artifact_id, record.content_hash),
    }
    if {(ref.artifact_id, ref.sha256) for ref in conform_map.inputs} != required:
        raise MapValidationError("inputs_mismatch", "envelope inputs do not bind sources")


def _validate_half_open_total(conform_map: ConformMap) -> None:
    table = conform_map.video_table
    if not table.rows or conform_map.normalization.dropped_source_frames:
        return
    time_base = table.rows[0].original_pts.time_base
    end = pts_floor_for_video_frame(table.source_frame_count, table.source_rate, time_base)
    whole = original_span_to_edit_span(
        conform_map, PtsSpan(start_pts=0, end_pts=end.pts, time_base=time_base)
    )
    if (whole.start_frame, whole.end_frame) != (0, table.output_frames):
        raise MapValidationError(
            "inconsistent_table",
            f"whole-source span maps to [{whole.start_frame}, {whole.end_frame})",
        )


def validate_conform_map(
    conform_map: ConformMap,
    *,
    source_manifest: SourceManifest | None = None,
    normalize_record: NormalizeRecord | None = None,
) -> None:
    _validate_table_hash(conform_map)
    _validate_row_shape(conform_map)
    _validate_source_partition(conform_map)
    _validate_audio_map(conform_map)
    _validate_half_open_total(conform_map)
    if source_manifest is not None and normalize_record is not None:
        _validate_against_sources(conform_map, source_manifest, normalize_record)
