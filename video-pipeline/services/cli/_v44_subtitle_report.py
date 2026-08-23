"""Runtime report models for the T12 Japanese subtitle proof run.

The ``SubtitleProofReport`` is a RUNTIME report under the run output dir —
NOT a new authoritative artifact (PRD v4.4 §0.2 caps new artifact types;
this stays outside the registry). Per PRD §7.4 the evidence-side
(transcript quality vs the corrected sample) and the subtitle-side (cue
layout quality) verdicts are SEPARATE top-level fields; no blended verdict
exists anywhere in this model.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from services.build.render_models import RenderPresetExpectation
from services.contracts.primitives import Sha256, StrictModel
from services.qc.models import (
    AudioThresholds,
    IrThresholds,
    PreviewThresholds,
    QcPolicy,
    SubtitleThresholds,
    VideoThresholds,
)


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


SUBTITLE_PROOF_THRESHOLD_VERSION = "qc-thresholds-subtitle-proof-v1"


def subtitle_proof_policy(*, max_lines: int, max_chars_per_line: int) -> QcPolicy:
    """The subtitle-side QC policy for the proof run.

    ``evaluate_cues`` consumes only ``subtitle.*`` and
    ``video.expectation.r_frame_rate``; every other threshold block carries
    the inert defaults of ``tests/qc/support.py`` to satisfy the strict
    policy model (they gate nothing in this harness).
    """
    policy = QcPolicy(
        schema_version="resolved-qc-policy-v1",
        threshold_version=SUBTITLE_PROOF_THRESHOLD_VERSION,
        subtitle=SubtitleThresholds(
            min_duration_frames=15,
            max_lines=max_lines,
            max_chars_per_line=max_chars_per_line,
            timing_tolerance_ms=40,
            track_required=True,
        ),
        video=VideoThresholds(
            black_min_duration_ms=1000,
            freeze_min_duration_ms=2000,
            expectation=RenderPresetExpectation(
                container_format_name="mov,mp4",
                video_codec="h264",
                width=640,
                height=360,
                r_frame_rate="30/1",
                pix_fmt="yuv420p",
                audio_codec="aac",
                audio_sample_rate=48000,
                audio_channels=2,
            ),
        ),
        audio=AudioThresholds(
            max_peak_mb=-1000,
            loudness_min_mlufs=-40000,
            loudness_max_mlufs=-5000,
            max_silence_ms=2000,
            expected_channels=2,
        ),
        preview=PreviewThresholds(
            require_binding=False, subtitle_expected=True, max_duration_drift_ms=50
        ),
        ir=IrThresholds(require_binding=False, expected_total_frames=None),
        required_capabilities=(),
        capability_matrix=None,
        policy_sha256="0" * 64,
    )
    return policy.model_copy(update={"policy_sha256": policy.content_hash()})


class AsrBlock(StrictModel):
    """How the transcript was obtained (live pinned whisper vs recorded replay)."""

    mode: Literal["live", "replay"]
    artifact_path: str = Field(min_length=1, strict=True)
    artifact_sha256: Sha256
    segment_count: int = Field(ge=1, strict=True)
    skipped_empty_segments: int = Field(ge=0, strict=True)
    cache_status: Literal["hit", "miss"] | None = None
    input_media_path: str | None = None
    input_media_sha256: Sha256 | None = None


class SampleBlock(StrictModel):
    """The corrected sample the evidence side was measured against."""

    path: str = Field(min_length=1, strict=True)
    sha256: Sha256
    entries: int = Field(ge=1, strict=True)
    proper_nouns: int = Field(ge=0, strict=True)


class EvidenceBlock(StrictModel):
    """Transcript-quality metrics ONLY (PRD §7.4 evidence side).

    Computed by T5's canonical ``compute_evidence_quality``; ``p95`` is
    null only when no hypothesis/reference utterance pair matched (an
    honest absence, never a fake zero).
    """

    metrics_source: str = Field(min_length=1, strict=True)
    transcript_cer: float = Field(ge=0, le=1)
    proper_noun_recall: float = Field(ge=0, le=1)
    timestamp_error_p95_ms: float | None = Field(default=None, ge=0)
    omitted_utterances: int = Field(ge=0, strict=True)
    duplicated_utterances: int = Field(ge=0, strict=True)


class SubtitleBlock(StrictModel):
    """Cue layout-quality results ONLY (PRD §7.4 presentation side)."""

    cue_count: int = Field(ge=1, strict=True)
    matrix_status: str = Field(min_length=1, strict=True)
    capability_path: str = Field(min_length=1, strict=True)
    snippet_render: Literal["external_srt"]
    snippet_path: str = Field(min_length=1, strict=True)
    snippet_sha256: Sha256
    qc_issue_count: int = Field(ge=0, strict=True)
    qc_rule_ids: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()
    reading_speed_violations: int = Field(ge=0, strict=True)
    legibility_note: str = Field(min_length=1, strict=True)


class SubtitleProofReport(StrictModel):
    """One proof run: evidence side and subtitle side kept strictly apart."""

    schema_version: Literal["v44-subtitle-proof-v1"]
    episode_id: str = Field(min_length=1, strict=True)
    evidence_verdict: Literal["measured"]
    subtitle_verdict: Literal["clean", "issues-found"]
    asr: AsrBlock
    evidence: EvidenceBlock
    subtitle: SubtitleBlock
    sample: SampleBlock
    notes: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()


__all__ = [
    "AsrBlock",
    "EvidenceBlock",
    "SampleBlock",
    "SubtitleBlock",
    "SubtitleProofReport",
    "subtitle_proof_policy",
]
