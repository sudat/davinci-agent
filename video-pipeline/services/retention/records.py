"""Decision and audit record models shared by planning and execution."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import Identifier, Sha256, StrictModel

AUDIT_SCHEMA = "gc-audit-v1"
SUMMARY_SCHEMA = "gc-summary-v1"

AuditReason = Literal[
    "eligible",
    "empty-dir-prune",
    "authoritative",
    "manual-finalization",
    "protected",
    "unclassified",
    "hold-active",
    "hold-needs-human",
    "hold-failed",
    "hold-missing-state",
    "retention-period",
    "symlink",
    "symlink-escape",
    "unregistered",
    "hash-mismatch",
    "drift-before-execute",
]

DecisionAction = Literal["planned_delete", "deleted", "retained"]
RunMode = Literal["dry_run", "execute"]


class Decision(StrictModel):
    """One retention decision about one path, relative to the jobs root."""

    job_id: Identifier
    path: str
    action: DecisionAction
    reason: AuditReason
    disk_sha256: Sha256 | None = None
    size_bytes: int = Field(default=0, ge=0, strict=True)
    registry_sha256: Sha256 | None = None
    symlink_target: str | None = None

    def audit_record(
        self, *, run_id: str, mode: RunMode, recorded_epoch_s: int
    ) -> AuditRecord:
        payload = self.model_dump(exclude={"action"})
        return AuditRecord(
            **payload,
            decision=self.action,
            run_id=run_id,
            mode=mode,
            recorded_epoch_s=recorded_epoch_s,
        )


class AuditRecord(StrictModel):
    """One append-only audit line: the decision plus the run header."""

    schema_version: Literal["gc-audit-v1"] = "gc-audit-v1"
    run_id: str = Field(min_length=1)
    mode: RunMode
    recorded_epoch_s: int = Field(ge=0, strict=True)
    decision: DecisionAction
    job_id: Identifier
    path: str
    reason: AuditReason
    disk_sha256: Sha256 | None = None
    size_bytes: int = Field(default=0, ge=0, strict=True)
    registry_sha256: Sha256 | None = None
    symlink_target: str | None = None


class GcSummary(StrictModel):
    schema_version: Literal["gc-summary-v1"] = "gc-summary-v1"
    mode: RunMode
    run_id: str = Field(min_length=1)
    jobs_seen: int = Field(ge=0, strict=True)
    planned_deletes: int = Field(ge=0, strict=True)
    deleted: int = Field(ge=0, strict=True)
    retained: int = Field(ge=0, strict=True)
    pruned_dirs: int = Field(ge=0, strict=True)
    audit_path: str
    now_epoch_s: int = Field(ge=0, strict=True)


__all__ = [
    "AUDIT_SCHEMA",
    "SUMMARY_SCHEMA",
    "AuditReason",
    "AuditRecord",
    "Decision",
    "DecisionAction",
    "GcSummary",
    "RunMode",
]
