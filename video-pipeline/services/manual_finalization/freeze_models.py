"""Strict models for the Manual Finalization Freeze Package (PRD 7.4).

The Freeze Package is the manual-finalization exception: when automation
cannot finish the episode, the operator freezes the job and the timeline
becomes the authoritative artifact. The package carries the deterministic
reproduction instructions (DRT/DRP: pinned toolchain lock, exact input
hashes, reproduction command list), the render reference, timeline and
conformance fingerprints, the manual change log, the last committed Plan,
a structured reason, the MANUAL_FREEZE operator record binding, and the
rights/privacy/QC report references. The package is hash-sealed; version
assignment and append-only storage live in ``services.manual_finalization.store``.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256
from services.foundation_io import canonical_model_bytes

FREEZE_PACKAGE_SCHEMA = "freeze-package-v1"

NonEmptyString = Annotated[str, Field(min_length=1, strict=True)]


class InputHash(StrictModel):
    name: NonEmptyString
    sha256: Sha256


class DrtDrp(StrictModel):
    """Deterministic reproduction instructions: pinned toolchain + inputs + commands."""

    toolchain_lock_sha256: Sha256
    input_hashes: tuple[InputHash, ...] = Field(min_length=1)
    reproduction_commands: tuple[NonEmptyString, ...] = Field(min_length=1)


class ChangeLogEntry(StrictModel):
    entry: NonEmptyString
    source_record: str | None = None


FreezeReasonCode = Literal[
    "unsupported_capability",
    "operator_choice",
    "retry_exhausted",
]


class StructuredReason(StrictModel):
    code: FreezeReasonCode
    detail: NonEmptyString


ReportKind = Literal["rights", "privacy", "qc"]


class ReportRef(StrictModel):
    kind: ReportKind
    sha256: Sha256


class OperatorRecordBinding(StrictModel):
    purpose: Literal["manual_freeze"]
    record_id: NonEmptyString
    fixture_only: bool


def _require_report_kinds(reports: tuple[ReportRef, ...]) -> None:
    kinds = {report.kind for report in reports}
    missing = {"rights", "privacy", "qc"} - kinds
    if missing:
        raise PydanticCustomError(
            "report_reference_missing",
            "a freeze package must reference the rights/privacy/QC reports; "
            "missing {missing}",
            {"missing": sorted(missing)},
        )

class FreezeInputs(StrictModel):
    episode_id: Identifier
    fixture_only: bool
    target_set_hash: Sha256
    drt_drp: DrtDrp
    render: InputHash
    timeline_fingerprint: Sha256
    conformance_fingerprint: Sha256
    change_log: tuple[ChangeLogEntry, ...] = Field(min_length=1)
    last_committed_plan: InputHash
    reports: tuple[ReportRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_report_references(self) -> FreezeInputs:
        _require_report_kinds(self.reports)
        return self


class FreezePackage(StrictModel):
    schema_version: Literal["freeze-package-v1"]
    episode_id: Identifier
    automation_frozen: Literal[True] = True
    fixture_only: bool
    drt_drp: DrtDrp
    render: InputHash
    timeline_fingerprint: Sha256
    conformance_fingerprint: Sha256
    change_log: tuple[ChangeLogEntry, ...] = Field(min_length=1)
    last_committed_plan: InputHash
    reason: StructuredReason
    operator_record: OperatorRecordBinding
    reports: tuple[ReportRef, ...] = Field(min_length=1)
    target_set_hash: Sha256
    package_sha256: Sha256

    @model_validator(mode="after")
    def require_report_references(self) -> FreezePackage:
        _require_report_kinds(self.reports)
        return self

    def computed_package_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"package_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()

    @model_validator(mode="after")
    def require_seal_consistent(self) -> FreezePackage:
        if self.package_sha256 != self.computed_package_hash():
            raise PydanticCustomError(
                "package_seal_inconsistent",
                "package_sha256 does not seal the package content",
            )
        return self


__all__ = [
    "FREEZE_PACKAGE_SCHEMA",
    "ChangeLogEntry",
    "DrtDrp",
    "FreezeInputs",
    "FreezePackage",
    "FreezeReasonCode",
    "InputHash",
    "OperatorRecordBinding",
    "ReportKind",
    "ReportRef",
    "StructuredReason",
]
