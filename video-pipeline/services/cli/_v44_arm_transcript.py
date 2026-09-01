"""Transcript lanes + fail-closed pre-model ASR alignment (Task 3).

Two explicit lanes feed the Director/MI text — the names are frozen product
vocabulary and can never share a label accidentally:

- ``operator_corrected_diagnostic`` — the operator's corrected sample IS the
  transcript: loaded, re-quantized through ``real_pool.segments_from_ms``
  (the SAME 30fps half-open lattice as the ASR path), SHA-recorded, and used
  as Director/MI text. DIAGNOSTIC evidence only, never product proof.
- ``system_asr`` — only the pinned ASR artifact (deterministic proper-noun
  substitution remains allowed). Any operator-corrected override flag is a
  typed ``asr-alignment-failed`` refusal, and the four predeclared metrics
  (CER, timestamp p95, omitted, duplicated — measured post-substitution,
  the director-visible transcript, matching the r3 measurement) must pass
  their FROZEN thresholds before ``build_speech_mi_artifact`` and every paid
  call. Any null metric is a failure; thresholds are never relaxed after
  observing results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, assert_never

from pydantic import ValidationError

from services.cli._v44_arm_proper_nouns import substituted_arm_transcript
from services.cli.real_pool import SpeechSegment, segments_from_ms
from services.cli.v44_arm_evidence import compute_arm_evidence_quality
from services.cli.v44_arm_stages import ArmPipelineData, ArmPipelineError
from services.metrics.v44_asr_measurement import POLICY_VERSION
from services.metrics.v44_product_proof import (
    EvidenceLane,
    EvidenceQualityMetrics,
    TranscriptSampleV1,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Frozen predeclared system-ASR readiness thresholds (plan §Predeclared
#: system-ASR readiness thresholds). Relaxing these after observing results
#: is forbidden; a metric that cannot be computed (null) is a failure.
CER_MAX: Final = 0.10
TIMESTAMP_P95_MS_MAX: Final = 500.0
OMITTED_MAX: Final = 5
DUPLICATED_MAX: Final = 5

#: The conventional corrected-reference location the system lane reads to
#: compute its alignment metrics (the reference, never the transcript).
_REFERENCE_NAME: Final = "transcript-sample-corrected.json"


@dataclass(frozen=True, slots=True)
class TranscriptLaneInput:
    """Explicit transcript lane/source wiring for one arm run.

    ``lane`` has NO default: every caller must declare which transcript
    bytes the run consumes. ``corrected_sample`` is REQUIRED for the
    diagnostic lane and is an override the system lane rejects. When the
    run carries Todo-1's evaluation binding, ``expected_sha256`` verifies
    the declared transcript hash against the effective one BEFORE any paid
    call.
    """

    lane: EvidenceLane
    corrected_sample: Path | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptAlignment:
    """Pre-model alignment facts about the effective transcript."""

    lane: EvidenceLane
    effective_transcript_sha256: str
    evidence_quality: EvidenceQualityMetrics
    provenance: tuple[str, ...]
    measurement_policy: str


def effective_transcript_sha256(
    hypothesis_ms: Sequence[tuple[int, int, str]],
) -> str:
    """SHA-256 over the canonical JSON dump of the effective triples.

    The bound bytes are exactly the (start_ms, end_ms, text) list whose text
    fed Director/MI and the metrics — deterministic, lane-agnostic format,
    recomputed on every run (never cached).
    """
    payload = json.dumps(
        [
            {"start_ms": start, "end_ms": end, "text": text}
            for start, end, text in hypothesis_ms
        ],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def enforce_system_asr_thresholds(metrics: EvidenceQualityMetrics) -> None:
    """Fail-closed check of the four predeclared thresholds; typed refusal."""
    checks: tuple[tuple[str, float | int | None, float | int], ...] = (
        ("transcript_cer", metrics.transcript_cer, CER_MAX),
        ("timestamp_error_p95_ms", metrics.timestamp_error_p95_ms, TIMESTAMP_P95_MS_MAX),
        ("omitted_utterances", metrics.omitted_utterances, OMITTED_MAX),
        ("duplicated_utterances", metrics.duplicated_utterances, DUPLICATED_MAX),
    )
    failures = [
        f"{name} is null (fail-closed)" if value is None else f"{name}={value!r} exceeds {limit!r}"
        for name, value, limit in checks
        if value is None or value > limit
    ]
    if failures:
        raise ArmPipelineError(
            "asr-alignment-failed",
            "system_asr transcript alignment failed the predeclared thresholds: "
            + "; ".join(failures),
        )


def _load_sample(path: Path) -> TranscriptSampleV1:
    """Fail-closed corrected-sample load (missing/unreadable/empty are typed)."""
    if not path.is_file():
        raise ArmPipelineError(
            "transcript-sample-missing",
            f"corrected transcript sample not found: {path}",
        )
    try:
        sample = TranscriptSampleV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise ArmPipelineError(
            "transcript-sample-unreadable",
            f"corrected transcript sample invalid at {path}: {error}",
        ) from error
    if not sample.segments:
        raise ArmPipelineError(
            "transcript-sample-unreadable",
            f"corrected transcript sample carries no segments: {path}",
        )
    return sample


def _system_reference(episode_root: Path) -> TranscriptSampleV1:
    """The corrected reference the system lane measures the pinned ASR by.

    A missing/unreadable reference means the four metrics cannot be computed
    — null metrics are failures, so this is itself an ``asr-alignment-failed``
    refusal (fail-closed, never a pass-by-absence).
    """
    path = episode_root / _REFERENCE_NAME
    try:
        return _load_sample(path)
    except ArmPipelineError as error:
        raise ArmPipelineError(
            "asr-alignment-failed",
            "system_asr requires the corrected reference sample "
            f"({path.name} under the episode root) to compute alignment "
            f"metrics — {error.detail}",
        ) from error

def prepare_arm_transcript(
    data: ArmPipelineData,
    episode_root: Path,
    lane_input: TranscriptLaneInput,
) -> tuple[tuple[SpeechSegment, ...], tuple[tuple[int, int, str], ...], int, TranscriptAlignment]:
    """Resolve the effective transcript for the declared lane.

    Returns the effective speech segments (MI/Director input), the effective
    ms hypothesis (metrics + binding), the applied proper-noun substitution
    count, and the alignment facts. For ``system_asr`` the frozen thresholds
    are enforced HERE — callers must run this before ``build_speech_mi_artifact``
    and before every paid call.
    """
    if lane_input.lane == "operator_corrected_diagnostic":
        if lane_input.corrected_sample is None:
            raise ArmPipelineError(
                "transcript-sample-missing",
                "the operator_corrected_diagnostic lane requires the corrected "
                "sample path (explicit input — no default, no fallback)",
            )
        sample = _load_sample(lane_input.corrected_sample)
        segments_ms = tuple(
            (int(segment.start_ms), int(segment.end_ms), segment.text)
            for segment in sample.segments
        )
        speech = segments_from_ms(segments_ms, data.total_frames)
        provenance = (
            (
                "transcript source: operator corrected sample, re-quantized "
                "through the shared 30fps half-open lattice"
            ),
            "operator provenance recorded — DIAGNOSTIC ONLY, non-product evidence",
        )
    elif lane_input.lane == "system_asr":
        if lane_input.corrected_sample is not None:
            raise ArmPipelineError(
                "asr-alignment-failed",
                f"operator-corrected transcript override {lane_input.corrected_sample} "
                "is rejected in the system_asr lane: corrected bytes may never "
                "enter the product transcript path (no alias, fallback, or "
                "partial-argument route)",
            )
        sample = _system_reference(episode_root)
        speech = data.speech
        segments_ms = data.transcript_segments_ms
        provenance = (
            (
                "transcript source: pinned ASR artifact (deterministic "
                "proper-noun substitution applied)"
            ),
        )
    else:
        assert_never(lane_input.lane)
    speech, hypothesis_ms, noun_edits = substituted_arm_transcript(
        speech, segments_ms, episode_root
    )
    alignment = TranscriptAlignment(
        lane=lane_input.lane,
        effective_transcript_sha256=effective_transcript_sha256(hypothesis_ms),
        evidence_quality=compute_arm_evidence_quality(sample, hypothesis_ms),
        provenance=provenance,
        measurement_policy=POLICY_VERSION,
    )
    if (
        lane_input.expected_sha256 is not None
        and lane_input.expected_sha256 != alignment.effective_transcript_sha256
    ):
        raise ArmPipelineError(
            "evaluation-binding-mismatch",
            f"declared transcript sha256 {lane_input.expected_sha256[:12]}... does "
            f"not equal the effective transcript sha256 "
            f"{alignment.effective_transcript_sha256[:12]}... — refusing before "
            "any paid call",
        )
    if lane_input.lane == "system_asr":
        enforce_system_asr_thresholds(alignment.evidence_quality)
    return speech, hypothesis_ms, noun_edits, alignment


__all__ = [
    "CER_MAX",
    "DUPLICATED_MAX",
    "OMITTED_MAX",
    "TIMESTAMP_P95_MS_MAX",
    "TranscriptAlignment",
    "TranscriptLaneInput",
    "effective_transcript_sha256",
    "enforce_system_asr_thresholds",
    "prepare_arm_transcript",
]
