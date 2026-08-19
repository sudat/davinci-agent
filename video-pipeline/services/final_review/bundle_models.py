"""Strict models for the sealed Final Review bundle.

Pure value models only: privacy entries with resolution state, the
editorial-checkpoint diff, and the hash-sealed ``FinalReviewBundle`` whose
``target_set_hash`` covers all bundle content (the displayed target set) and
whose ``bundle_sha256`` seals it. Assembly and privacy alignment live in
``services.final_review.bundle``.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256
from services.foundation_io import canonical_model_bytes

FINAL_REVIEW_BUNDLE_SCHEMA = "final-review-bundle-v1"

ResolutionState = Literal[
    "unresolved",
    "cleared_for_publication",
    "removed_from_output",
    "masked_in_output",
]


class BundlePrivacyEntry(StrictModel):
    """One manually-declared privacy/rights entry with resolution state."""

    issue_id: Identifier
    category: Literal["privacy", "rights"]
    declared_by: str = Field(min_length=1, strict=True)
    detail: str = Field(min_length=1, strict=True)
    resolution_state: ResolutionState
    resolution_record_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def require_resolution_record_when_resolved(self) -> BundlePrivacyEntry:
        if self.resolution_state == "unresolved":
            if self.resolution_record_sha256 is not None:
                raise PydanticCustomError(
                    "resolution_record_forbidden",
                    "an unresolved entry cannot carry a resolution record",
                )
            return self
        if self.resolution_record_sha256 is None:
            raise PydanticCustomError(
                "resolution_record_missing",
                "a resolved entry requires the operator resolution record sha256",
            )
        return self


class DiffComponent(StrictModel):
    component: Literal["plan", "preview", "render"]
    checkpoint_sha256: Sha256
    final_sha256: Sha256


class EditorialDiff(StrictModel):
    """Diff hashes vs the EDITORIAL_APPROVED checkpoint bundle."""

    checkpoint_target_set_hash: Sha256
    components: tuple[DiffComponent, ...] = ()


class FinalReviewBundle(StrictModel):
    schema_version: Literal["final-review-bundle-v1"]
    episode_id: Identifier
    fixture_only: bool
    render_sha256: Sha256
    build_output_sha256: Sha256
    conformance_fingerprint: Sha256
    qc_report_sha256: Sha256
    qc_verdict: Literal["passed", "blocked"]
    privacy_rights: tuple[BundlePrivacyEntry, ...] = ()
    editorial_diff: EditorialDiff
    target_set_hash: Sha256
    bundle_sha256: Sha256

    def computed_target_set_hash(self) -> Sha256:
        zeroed = self.model_copy(
            update={"target_set_hash": GENESIS_SHA256, "bundle_sha256": GENESIS_SHA256}
        )
        return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()

    def computed_bundle_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"bundle_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()

    def verify_seals(self) -> bool:
        return (
            self.target_set_hash == self.computed_target_set_hash()
            and self.bundle_sha256 == self.computed_bundle_hash()
        )

    @model_validator(mode="after")
    def require_seals_consistent(self) -> FinalReviewBundle:
        if self.target_set_hash != self.computed_target_set_hash():
            raise PydanticCustomError(
                "target_set_seal_inconsistent",
                "the displayed target-set hash does not cover the bundle content",
            )
        if self.bundle_sha256 != self.computed_bundle_hash():
            raise PydanticCustomError(
                "bundle_seal_inconsistent",
                "bundle_sha256 does not seal the bundle content",
            )
        return self


__all__ = [
    "FINAL_REVIEW_BUNDLE_SCHEMA",
    "BundlePrivacyEntry",
    "DiffComponent",
    "EditorialDiff",
    "FinalReviewBundle",
    "ResolutionState",
]
