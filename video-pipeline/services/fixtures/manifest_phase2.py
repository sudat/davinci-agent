"""Phase-2 fixture manifest models (five Phase-1-reference-derived faults).

Every manifest reuses ONE frozen Phase-1 Reference scenario (declared base
record table + edit source, copied from the frozen Phase-1 goldens) and
injects exactly ONE named fault with its full expected
Package/readback/render/QC/retry/human routing pre-registered. Nothing in
this manifest family is derived from production code; the independent
Golden derivation (``tests/goldens/reference/phase-2``) restates every
declared value and derives the routing rationally.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

PHASE_2_FIXTURE_IDS: tuple[str, ...] = (
    "p2-stale-capability",
    "p2-partial-build-restart",
    "p2-same-duration-wrong-media",
    "p2-false-render-complete",
    "p2-blocking-qc-privacy",
)

PHASE_2_DERIVED_FROM: dict[str, str] = {
    "p2-stale-capability": "p1-ref-01-clean-ja",
    "p2-partial-build-restart": "p1-ref-02-pauses-fillers",
    "p2-same-duration-wrong-media": "p1-ref-03-multi-take-must-include",
    "p2-false-render-complete": "p1-ref-04-linked-av-offset",
    "p2-blocking-qc-privacy": "p1-ref-05-review-mix",
}

type RetryRoute = Literal[
    "none-input-fix",
    "clean-rebuild-restart",
    "bounded-auto-retry",
    "none-blocking",
]

type HumanRoute = Literal[
    "none",
    "refresh-capability-matrix",
    "media-source-review",
    "render-job-review",
    "privacy-dismissal-required",
]


class BaseRecordRow(StrictModel):
    """One pre-registered base IR record (copied from the Phase-1 golden)."""

    item_id: Identifier
    kind: Literal["video", "audio", "subtitle"]
    track_index: int = Field(gt=0, strict=True)
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    av_link_id: Identifier | None = None
    segment_id: Identifier | None = None
    subtitle_text: str | None = None

    @model_validator(mode="after")
    def require_consistent_row(self) -> BaseRecordRow:
        if self.record_end <= self.record_start or self.source_end <= self.source_start:
            raise PydanticCustomError("row_span", "base rows carry non-empty spans")
        if (self.kind == "subtitle") != (self.subtitle_text is not None):
            raise PydanticCustomError("row_text", "only subtitle rows carry text")
        if self.kind not in ("video", "audio") and self.av_link_id is not None:
            raise PydanticCustomError("row_link", "only A/V rows carry a link id")
        return self


class MediaBindingSpec(StrictModel):
    """The declared edit-source media binding presented to package compile."""

    source_id: Identifier
    path: str
    sha256: Sha256
    duration_frames: int = Field(gt=0, strict=True)


class BaseScenario(StrictModel):
    derived_from: str
    source_id: Identifier
    total_frames: int = Field(gt=0, strict=True)
    frame_rate_num: Literal[30]
    frame_rate_den: Literal[1]
    audio_sample_rate: Literal[48000]
    base_records: tuple[BaseRecordRow, ...] = Field(min_length=1)
    declared_media: MediaBindingSpec

    @model_validator(mode="after")
    def require_declared_media_covers_extent(self) -> BaseScenario:
        if self.declared_media.source_id != self.source_id:
            raise PydanticCustomError("media_source", "declared media binds the base source")
        if any(
            row.source_end > self.declared_media.duration_frames for row in self.base_records
        ):
            raise PydanticCustomError("media_extent", "declared media shorter than base rows")
        return self


class StaleCapabilityFault(StrictModel):
    kind: Literal["stale-capability"] = "stale-capability"
    declared_matrix_sha256: Sha256
    expected_package_compilation: Literal["typed-failure"] = "typed-failure"
    expected_failure_code: Literal["capability-matrix-stale"] = "capability-matrix-stale"
    expected_readback: Literal["not-attempted"] = "not-attempted"
    expected_render: Literal["not-attempted"] = "not-attempted"
    expected_qc: Literal["not-attempted"] = "not-attempted"
    expected_retry: Literal["none-input-fix"] = "none-input-fix"
    expected_human_route: Literal["refresh-capability-matrix"] = "refresh-capability-matrix"
    fault_description: str = Field(min_length=1)


class PartialBuildRestartFault(StrictModel):
    kind: Literal["partial-build-restart"] = "partial-build-restart"
    interrupt_after_placed_items: int = Field(gt=0, strict=True)
    expected_package_compilation: Literal["succeeds"] = "succeeds"
    expected_readback: Literal["partial-then-clean-rebuild"] = "partial-then-clean-rebuild"
    expected_render: Literal["verified-after-restart"] = "verified-after-restart"
    expected_qc: Literal["deterministic-after-restart"] = "deterministic-after-restart"
    expected_retry: Literal["clean-rebuild-restart"] = "clean-rebuild-restart"
    expected_human_route: Literal["none"] = "none"
    fault_description: str = Field(min_length=1)


class SameDurationWrongMediaFault(StrictModel):
    kind: Literal["same-duration-wrong-media"] = "same-duration-wrong-media"
    substituted_source_id: Identifier
    presented_media_sha256: Sha256
    presented_duration_frames: int = Field(gt=0, strict=True)
    expected_package_compilation: Literal["typed-failure"] = "typed-failure"
    expected_failure_code: Literal["media-hash-drift"] = "media-hash-drift"
    expected_readback: Literal["not-attempted"] = "not-attempted"
    expected_render: Literal["blocked"] = "blocked"
    expected_qc: Literal["blocked"] = "blocked"
    expected_retry: Literal["none-input-fix"] = "none-input-fix"
    expected_human_route: Literal["media-source-review"] = "media-source-review"
    fault_description: str = Field(min_length=1)


class FalseRenderCompleteFault(StrictModel):
    kind: Literal["false-render-complete"] = "false-render-complete"
    reported_job_status: str = Field(min_length=1)
    reported_completion_percentage: int = Field(ge=0, le=99, strict=True)
    expected_package_compilation: Literal["succeeds"] = "succeeds"
    expected_readback: Literal["conformant"] = "conformant"
    expected_render: Literal["refused-incomplete"] = "refused-incomplete"
    expected_failure_code: Literal["render-incomplete"] = "render-incomplete"
    expected_qc: Literal["blocked"] = "blocked"
    expected_retry: Literal["bounded-auto-retry"] = "bounded-auto-retry"
    expected_human_route: Literal["render-job-review"] = "render-job-review"
    fault_description: str = Field(min_length=1)


class BlockingQcPrivacyFault(StrictModel):
    kind: Literal["blocking-qc-privacy"] = "blocking-qc-privacy"
    flagged_segment_id: Identifier
    privacy_flag: Identifier
    expected_package_compilation: Literal["succeeds"] = "succeeds"
    expected_readback: Literal["conformant"] = "conformant"
    expected_render: Literal["verified"] = "verified"
    expected_qc: Literal["typed-failure-blocks-publish"] = "typed-failure-blocks-publish"
    expected_failure_code: Literal["privacy-flag-blocks-publish"] = (
        "privacy-flag-blocks-publish"
    )
    expected_retry: Literal["none-blocking"] = "none-blocking"
    expected_human_route: Literal["privacy-dismissal-required"] = "privacy-dismissal-required"
    fault_description: str = Field(min_length=1)


Phase2Fault = Annotated[
    StaleCapabilityFault
    | PartialBuildRestartFault
    | SameDurationWrongMediaFault
    | FalseRenderCompleteFault
    | BlockingQcPrivacyFault,
    Field(discriminator="kind"),
]


class Phase2FixtureManifest(StrictModel):
    schema_version: Literal["phase-2-fixture-manifest-v1"]
    phase: Literal["phase-2"]
    fixture_id: str
    fixture_only: Literal[True]
    expectation_basis: Literal["pre-registered-phase1-derived-fault-injection"]
    base: BaseScenario
    fault: Phase2Fault

    @model_validator(mode="after")
    def require_derivation_shape(self) -> Phase2FixtureManifest:
        if self.base.derived_from != PHASE_2_DERIVED_FROM.get(self.fixture_id):
            raise PydanticCustomError(
                "derived_from",
                "fixture {fixture} must derive from {expected}",
                {
                    "fixture": self.fixture_id,
                    "expected": PHASE_2_DERIVED_FROM.get(self.fixture_id, "unknown"),
                },
            )
        if isinstance(self.fault, PartialBuildRestartFault) and (
            self.fault.interrupt_after_placed_items >= len(self.base.base_records)
        ):
            raise PydanticCustomError(
                "interrupt_partial", "restart interrupt must leave the build partial"
            )
        if isinstance(self.fault, SameDurationWrongMediaFault) and (
            self.fault.substituted_source_id != self.base.declared_media.source_id
            or self.fault.presented_duration_frames != self.base.declared_media.duration_frames
            or self.fault.presented_media_sha256 == self.base.declared_media.sha256
        ):
            raise PydanticCustomError(
                "fault_shape",
                "wrong-media fault keeps duration but swaps bytes",
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)


__all__ = [
    "PHASE_2_DERIVED_FROM",
    "PHASE_2_FIXTURE_IDS",
    "Phase2FixtureManifest",
]
