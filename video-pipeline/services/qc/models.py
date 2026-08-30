"""Strict QC models: issues, report, policy.

Issues carry rule id, severity, Decision/record-span binding, RAW measured
evidence with tool/threshold versions, and input hashes. The report
structurally refuses ``verdict=passed`` with any blocker or unresolved human
gate. The policy is the canonical resolved-policy-family artifact: versioned
thresholds hashed into ``policy_sha256``.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.build.render_models import (  # noqa: TC001 (pydantic resolves it at runtime)
    RenderPresetExpectation,
)
from services.contracts.primitives import (
    Identifier,
    RecordFrameSpan,
    Sha256,
    StrictModel,
)
from services.contracts.serialization import GENESIS_SHA256
from services.foundation_io import canonical_model_bytes

QC_ENGINE_VERSION: Final = "todo52-v1"

QcVerdict = Literal["passed", "blocked"]
QcSeverity = Literal["blocker", "major", "minor"]
QcInputKind = Literal["render", "policy", "ir", "preview", "analysis", "privacy",
                      "source_manifest", "edit_source"]

QcRuleId = Literal[
    # IR conformance (Todo-44 ids reused where they overlap)
    "ir_binding_missing",
    "ir_span_gap",
    "ir_span_overlap",
    "ir_totals_mismatch",
    "ir_link_mismatch",
    "track_kind_mismatch",
    "resolve_field_forbidden",
    # Preview
    "preview_binding_missing",
    "preview_duration_drift",
    "preview_rate_drift",
    "preview_trace_coverage",
    "preview_subtitle_presence",
    # Final-render video
    "video_decode_failed",
    "video_black_span",
    "video_freeze_span",
    "video_metadata_mismatch",
    "video_orientation_mismatch",
    "video_orientation_unverified",
    "qc_capability_unsupported",
    # Final-render audio (mB / mLU integer encodings)
    "audio_channels_mismatch",
    "audio_peak_over",
    "audio_loudness_out_of_range",
    "audio_loudness_unmeasured",
    "audio_silence_excess",
    # Subtitles (Todo-44 rule set on the demuxed track)
    "subtitle_track_missing",
    "subtitle_min_duration",
    "subtitle_max_lines",
    "subtitle_max_chars",
    "subtitle_safe_area",
    "subtitle_cue_overlap",
    "subtitle_timing_drift",
    "subtitle_text_drift",
    # Manually declared Privacy/Rights (gate, never detection)
    "privacy_rights_unresolved",
    "privacy_rights_resolved",
]

# Frozen severity table: every violation blocks except the recorded
# resolution marker, which is minor evidence.
_BLOCKER_RULES: Final[frozenset[str]] = frozenset({"privacy_rights_resolved"})


def rule_severity(rule_id: QcRuleId) -> QcSeverity:
    return "minor" if rule_id in _BLOCKER_RULES else "blocker"


class QcMeasured(StrictModel):
    """One raw measured value exactly as the tool reported it."""

    name: str = Field(min_length=1, strict=True)
    value: str = Field(min_length=1, strict=True)


class QcEvidence(StrictModel):
    measured: tuple[QcMeasured, ...]
    tool_version: str = Field(min_length=1, strict=True)
    threshold_version: str = Field(min_length=1, strict=True)


class QcInputBinding(StrictModel):
    kind: QcInputKind
    sha256: Sha256


class QcIssue(StrictModel):
    rule_id: QcRuleId
    severity: QcSeverity
    detail: str = Field(min_length=1, strict=True)
    item_id: Identifier | None = None
    decision_id: str | None = None
    record_span: RecordFrameSpan | None = None
    evidence: QcEvidence
    input_hashes: tuple[Sha256, ...] = Field(min_length=1)


class UnresolvedHumanGate(StrictModel):
    gate_id: Identifier
    rule_id: QcRuleId
    source: Literal["privacy_declaration"]
    detail: str = Field(min_length=1, strict=True)


class QcToolVersions(StrictModel):
    qc_engine: str = Field(min_length=1, strict=True)
    ffmpeg_sha256: Sha256
    ffprobe_sha256: Sha256


class QcReport(StrictModel):
    schema_version: Literal["qc-report-v1"]
    verdict: QcVerdict
    issues: tuple[QcIssue, ...] = ()
    unresolved_human_gates: tuple[UnresolvedHumanGate, ...] = ()
    tool_versions: QcToolVersions
    threshold_version: str = Field(min_length=1, strict=True)
    inputs: tuple[QcInputBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_verdict_matches_findings(self) -> QcReport:
        unresolved_blockers = [i.rule_id for i in self.issues if i.severity == "blocker"]
        if self.verdict == "passed" and (unresolved_blockers or self.unresolved_human_gates):
            raise PydanticCustomError(
                "verdict_invalid",
                "verdict passed is impossible with blockers {blockers} or unresolved "
                "human gates {gates}",
                {
                    "blockers": unresolved_blockers,
                    "gates": [g.gate_id for g in self.unresolved_human_gates],
                },
            )
        return self


def compute_verdict(
    issues: tuple[QcIssue, ...], gates: tuple[UnresolvedHumanGate, ...]
) -> QcVerdict:
    if any(issue.severity == "blocker" for issue in issues) or gates:
        return "blocked"
    return "passed"


class SubtitleThresholds(StrictModel):
    min_duration_frames: int = Field(gt=0, strict=True)
    max_lines: int = Field(gt=0, strict=True)
    max_chars_per_line: int = Field(gt=0, strict=True)
    timing_tolerance_ms: int = Field(ge=0, strict=True)
    track_required: bool


class VideoThresholds(StrictModel):
    black_min_duration_ms: int = Field(gt=0, strict=True)
    freeze_min_duration_ms: int = Field(gt=0, strict=True)
    expectation: RenderPresetExpectation


class AudioThresholds(StrictModel):
    max_peak_mb: int = Field(le=0, strict=True)
    loudness_min_mlufs: int = Field(le=0, strict=True)
    loudness_max_mlufs: int = Field(le=0, strict=True)
    max_silence_ms: int = Field(gt=0, strict=True)
    expected_channels: int = Field(gt=0, strict=True)


class PreviewThresholds(StrictModel):
    require_binding: bool
    subtitle_expected: bool
    max_duration_drift_ms: int = Field(ge=0, strict=True)


class IrThresholds(StrictModel):
    require_binding: bool
    expected_total_frames: int | None = Field(gt=0, strict=True, default=None)


class CapabilityMatrixBinding(StrictModel):
    path: str = Field(min_length=1, strict=True)
    sha256: Sha256


class CapabilityRecord(StrictModel):
    capability: Identifier
    api_available: bool
    live_verified: bool


class QcPolicy(StrictModel):
    """Canonical resolved QC thresholds (hash over canonical bytes)."""

    schema_version: Literal["resolved-qc-policy-v1"]
    threshold_version: Identifier
    subtitle: SubtitleThresholds
    video: VideoThresholds
    audio: AudioThresholds
    preview: PreviewThresholds
    ir: IrThresholds
    required_capabilities: tuple[Identifier, ...] = ()
    capability_matrix: CapabilityMatrixBinding | None = None
    policy_sha256: Sha256

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"policy_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.policy_sha256 == self.content_hash()


__all__ = [
    "QC_ENGINE_VERSION",
    "CapabilityMatrixBinding",
    "CapabilityRecord",
    "IrThresholds",
    "PreviewThresholds",
    "QcEvidence",
    "QcInputBinding",
    "QcInputKind",
    "QcIssue",
    "QcMeasured",
    "QcPolicy",
    "QcReport",
    "QcRuleId",
    "QcSeverity",
    "QcToolVersions",
    "QcVerdict",
    "SubtitleThresholds",
    "UnresolvedHumanGate",
    "VideoThresholds",
    "compute_verdict",
    "rule_severity",
]
