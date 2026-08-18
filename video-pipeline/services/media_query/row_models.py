"""Typed row models for the media-query index tables.

Row-level facts mirror the Todo 33-35 artifact models exactly: canonical
fields never use floats (mB / mLU / samples / counts are ints), spans stay
half-open, and ``None`` marks a fact the producing analyzer genuinely does
not assert (e.g. a measured silence span carries no candidate confidence or
rule id).
"""

from __future__ import annotations

from services.contracts.primitives import StrictModel

AUDIO_SOURCE_ID = "edit-source-audio"
VIDEO_SOURCE_ID = "edit-source-video"


class SourceRow(StrictModel):
    source_id: str
    sha256: str
    analyzer_version: str
    artifact_sha: str
    path: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    rate_num: int | None = None
    rate_den: int | None = None
    frame_count: int | None = None


class TranscriptSegmentRow(StrictModel):
    source_id: str
    segment_index: int
    start_ms: int
    end_ms: int
    text: str
    analyzer_version: str
    artifact_sha: str
    confidence: int | None = None


class SampleSpanRow(StrictModel):
    source_id: str
    kind: str
    segment_index: int
    start_sample: int
    end_sample: int
    sample_rate: int
    start_ms: int
    end_ms: int
    text: str
    experimental: bool
    analyzer_version: str
    artifact_sha: str


class SilenceRangeRow(StrictModel):
    source_id: str
    kind: str
    start_sample: int
    end_sample: int
    sample_rate: int
    start_ms: int
    end_ms: int
    analyzer_version: str
    artifact_sha: str
    confidence: int | None = None
    rule_id: str | None = None


class QualityRangeRow(StrictModel):
    source_id: str
    kind: str
    start_frame: int
    end_frame: int
    confidence: int
    rule_id: str
    analyzer_version: str
    artifact_sha: str


class ContactRefRow(StrictModel):
    source_id: str
    sheet_path: str
    sheet_sha256: str
    frame_indexes: tuple[int, ...]
    cols: int
    rows: int
    thumb_w: int
    thumb_h: int
    generator: str
    cadence_frames: int
    analyzer_version: str
    artifact_sha: str


class StatisticRow(StrictModel):
    source_id: str
    metric: str
    value: int
    unit: str
    analyzer_version: str
    artifact_sha: str


class LineageRow(StrictModel):
    artifact_id: str
    artifact_type: str
    schema_version: str
    artifact_sha: str
    producer_name: str
    analyzer_version: str
    row_count: int



__all__ = [
    "AUDIO_SOURCE_ID",
    "VIDEO_SOURCE_ID",
    "ContactRefRow",
    "LineageRow",
    "QualityRangeRow",
    "SampleSpanRow",
    "SilenceRangeRow",
    "SourceRow",
    "StatisticRow",
    "TranscriptSegmentRow",
]
