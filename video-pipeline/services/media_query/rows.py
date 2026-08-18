"""Typed index rows extracted from the authoritative analyzer artifacts.

Filler and false-start candidates stay queryable only through their canonical
artifact; the index never invents a home for them. ``None`` marks facts the
producing analyzer genuinely does not assert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.analyze.asr_models import TranscriptArtifact
from services.analyze.candidate_models import AnalysisArtifact
from services.analyze.visual_models import VisualAnalysisArtifact
from services.analyze.visual_results import BlackSpanResult, BlurSpanResult, ExposureSpanResult
from services.media_query.row_models import (
    AUDIO_SOURCE_ID,
    VIDEO_SOURCE_ID,
    ContactRefRow,
    LineageRow,
    QualityRangeRow,
    SampleSpanRow,
    SilenceRangeRow,
    SourceRow,
    StatisticRow,
    TranscriptSegmentRow,
)

if TYPE_CHECKING:
    from services.analyze.analysis_models import SampleMsSpan

AnalyzerArtifact = TranscriptArtifact | AnalysisArtifact | VisualAnalysisArtifact
VisualEvidenceSpan = BlackSpanResult | BlurSpanResult | ExposureSpanResult


@dataclass(frozen=True, slots=True)
class IndexRowSet:
    sources: tuple[SourceRow, ...] = ()
    transcript_segments: tuple[TranscriptSegmentRow, ...] = ()
    sample_spans: tuple[SampleSpanRow, ...] = ()
    silence_ranges: tuple[SilenceRangeRow, ...] = ()
    quality_ranges: tuple[QualityRangeRow, ...] = ()
    contact_refs: tuple[ContactRefRow, ...] = ()
    statistics: tuple[StatisticRow, ...] = ()

    def row_count(self) -> int:
        return sum(len(rows) for rows in (
            self.sources, self.transcript_segments, self.sample_spans,
            self.silence_ranges, self.quality_ranges, self.contact_refs,
            self.statistics,
        ))


def lineage_row(artifact: AnalyzerArtifact, artifact_sha: str, row_count: int) -> LineageRow:
    return LineageRow(
        artifact_id=artifact.artifact_id, artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version, artifact_sha=artifact_sha,
        producer_name=artifact.producer.name,
        analyzer_version=artifact.producer.version, row_count=row_count)


def _audio_source_row(  # noqa: PLR0913, PLR0917 (mirrors the table row arity)
    path: str | None, sha256: str, sample_rate: int,
    channels: int, codec: str, version: str, artifact_sha: str,
) -> SourceRow:
    return SourceRow(
        source_id=AUDIO_SOURCE_ID, sha256=sha256, path=path,
        sample_rate=sample_rate, channels=channels, codec=codec,
        analyzer_version=version, artifact_sha=artifact_sha,
    )


def _sample_span_row(  # noqa: PLR0913, PLR0917 (mirrors the table row arity)
    kind: str, segment_index: int, span: SampleMsSpan, text: str,
    version: str, artifact_sha: str, *, experimental: bool,
) -> SampleSpanRow:
    return SampleSpanRow(
        source_id=AUDIO_SOURCE_ID, kind=kind, segment_index=segment_index,
        start_sample=span.start_sample, end_sample=span.end_sample,
        sample_rate=span.sample_rate, start_ms=span.start_ms, end_ms=span.end_ms,
        text=text, experimental=experimental,
        analyzer_version=version, artifact_sha=artifact_sha,
    )


def _silence_row(  # noqa: PLR0913, PLR0917 (mirrors the table row arity)
    kind: str, span: SampleMsSpan, confidence: int | None,
    rule_id: str | None, version: str, artifact_sha: str,
) -> SilenceRangeRow:
    return SilenceRangeRow(
        source_id=AUDIO_SOURCE_ID, kind=kind,
        start_sample=span.start_sample, end_sample=span.end_sample,
        sample_rate=span.sample_rate, start_ms=span.start_ms, end_ms=span.end_ms,
        confidence=confidence, rule_id=rule_id,
        analyzer_version=version, artifact_sha=artifact_sha,
    )


def _quality_row(
    kind: str, span: VisualEvidenceSpan, version: str, artifact_sha: str
) -> QualityRangeRow:
    return QualityRangeRow(
        source_id=VIDEO_SOURCE_ID, kind=kind,
        start_frame=span.span.start_frame, end_frame=span.span.end_frame,
        confidence=span.confidence, rule_id=span.provenance.rule_id,
        analyzer_version=version, artifact_sha=artifact_sha,
    )


def _transcript_rows(artifact: TranscriptArtifact, artifact_sha: str) -> IndexRowSet:
    version = artifact.producer.version
    binding = artifact.input_binding
    probe = binding.wav_probe
    source = _audio_source_row(
        None, binding.wav_sha256, probe.sample_rate, probe.channels, probe.codec,
        version, artifact_sha,
    )
    segments = tuple(
        TranscriptSegmentRow(
            source_id=AUDIO_SOURCE_ID, segment_index=index,
            start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text,
            analyzer_version=version, artifact_sha=artifact_sha)
        for index, segment in enumerate(artifact.segments)
    )
    return IndexRowSet(sources=(source,), transcript_segments=segments)


def _pause_rows(artifact: AnalysisArtifact, artifact_sha: str) -> tuple[SilenceRangeRow, ...]:
    version = artifact.producer.version
    measured = tuple(
        _silence_row("measured_silence", span, None, None, version, artifact_sha)
        for span in artifact.measurements.silence_spans
    )
    pauses = tuple(
        _silence_row(
            "pause", candidate.span, candidate.confidence,
            candidate.provenance.rule_id, version, artifact_sha,
        )
        for candidate in artifact.candidates
        if candidate.kind == "pause"
    )
    return measured + pauses


def _statistics_rows(
    artifact: AnalysisArtifact, artifact_sha: str
) -> tuple[StatisticRow, ...]:
    version = artifact.producer.version
    measurements = artifact.measurements
    loudness = measurements.loudness
    dialogue = artifact.dialogue_ambient
    metric_values: dict[str, tuple[int, str]] = {
        "peak_sample": (measurements.peak_sample, "sample"),
        "peak_mb": (measurements.peak_mb, "mb"),
        "clipping_count": (measurements.clipping_count, "count"),
        "frame_count": (measurements.frame_count, "sample"),
        "rms_mean_mb": (loudness.rms_mean_mb, "mb"),
        "dialogue_rms_mb": (dialogue.dialogue_rms_mb, "mb"),
        "ambient_noise_floor_mb": (dialogue.ambient_noise_floor_mb, "mb"),
        "dialogue_sample_count": (dialogue.dialogue_sample_count, "sample"),
        "ambient_sample_count": (dialogue.ambient_sample_count, "sample"),
    }
    if loudness.integrated_loudness_mlufs is not None:
        metric_values["integrated_loudness_mlufs"] = (
            loudness.integrated_loudness_mlufs, "mlu")
    return tuple(
        StatisticRow(
            source_id=AUDIO_SOURCE_ID, metric=metric, value=value, unit=unit,
            analyzer_version=version, artifact_sha=artifact_sha)
        for metric, (value, unit) in metric_values.items()
    )


def _analysis_rows(artifact: AnalysisArtifact, artifact_sha: str) -> IndexRowSet:
    version = artifact.producer.version
    facts = artifact.probe.facts
    if facts is None:
        raise ValueError("analysis artifact with decode_ok must carry stream facts")
    source = _audio_source_row(
        artifact.wav.wav_path, artifact.wav.wav_sha256,
        facts.sample_rate, facts.channels, facts.codec, version, artifact_sha,
    )
    transcript_map = artifact.transcript_map
    segment_spans = tuple(
        _sample_span_row(
            "segment", segment.index, segment.span, segment.text,
            version, artifact_sha, experimental=False,
        )
        for segment in transcript_map.segments
    )
    # experimental token timing may repeat a (segment, start) lattice point;
    # the index key allows one row per point — first token wins, index-only
    token_spans: list[SampleSpanRow] = []
    seen_token_keys: set[tuple[int, int]] = set()
    for token in transcript_map.token_spans:
        key = (token.segment_index, token.span.start_sample)
        if key in seen_token_keys:
            continue
        seen_token_keys.add(key)
        token_spans.append(
            _sample_span_row(
                "token", token.segment_index, token.span, token.text,
                version, artifact_sha, experimental=True,
            )
        )
    return IndexRowSet(
        sources=(source,), sample_spans=(*segment_spans, *token_spans),
        silence_ranges=_pause_rows(artifact, artifact_sha),
        statistics=_statistics_rows(artifact, artifact_sha))


def _visual_rows(artifact: VisualAnalysisArtifact, artifact_sha: str) -> IndexRowSet:
    version = artifact.producer.version
    facts = artifact.media.facts
    source = SourceRow(
        source_id=VIDEO_SOURCE_ID, sha256=artifact.media.media_sha256,
        path=artifact.media.media_path, width=facts.width, height=facts.height,
        rate_num=facts.rate_num, rate_den=facts.rate_den,
        frame_count=facts.frame_count, analyzer_version=version,
        artifact_sha=artifact_sha,
    )
    quality = (
        tuple(_quality_row("black", span, version, artifact_sha) for span in artifact.black_spans)
        + tuple(_quality_row("blur", span, version, artifact_sha) for span in artifact.blur_spans)
        + tuple(
            _quality_row(
                "exposure_over" if span.direction == "over" else "exposure_under",
                span,
                version,
                artifact_sha,
            )
            for span in artifact.exposure_spans
        )
    )
    contacts = tuple(
        ContactRefRow(
            source_id=VIDEO_SOURCE_ID, sheet_path=sheet.path,
            sheet_sha256=sheet.sha256, frame_indexes=tuple(sheet.frame_indexes),
            cols=sheet.cols, rows=sheet.rows, thumb_w=sheet.thumb_w,
            thumb_h=sheet.thumb_h, generator=sheet.generator,
            cadence_frames=sheet.cadence_frames, analyzer_version=version,
            artifact_sha=artifact_sha)
        for sheet in artifact.sheets
    )
    return IndexRowSet(
        sources=(source,), quality_ranges=quality, contact_refs=contacts)


def rows_for_artifact(artifact: AnalyzerArtifact, artifact_sha: str) -> IndexRowSet:
    if isinstance(artifact, TranscriptArtifact):
        return _transcript_rows(artifact, artifact_sha)
    if isinstance(artifact, AnalysisArtifact):
        return _analysis_rows(artifact, artifact_sha)
    return _visual_rows(artifact, artifact_sha)


__all__ = ["AnalyzerArtifact", "IndexRowSet", "lineage_row", "rows_for_artifact"]
