"""Models for purpose-bound local operation records.

Canonical ordering authority is ``timestamp_seq`` (a logical clock
assigned by the append-only store). ``wall_time_unix`` is optional,
advisory display metadata only, and is never used for ordering,
validity, or authorization decisions. Floats are rejected everywhere
(strict models with strict ints).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

# Runtime import is required: pydantic resolves FinalReviewBinding while
# building OperationDraft fields, so a TYPE_CHECKING-only import breaks it.
from services.approvals.global_review_models import FinalReviewBinding  # noqa: TC001
from services.contracts.primitives import (
    Identifier,
    PositiveInteger,
    Sha256,
    StrictModel,
)
from services.contracts.serialization import canonical_json_bytes

ApprovalPurpose = Literal[
    "editorial",
    "presentation",
    "privacy",
    "rights",
    "final",
    "publication",
    "manual_freeze",
]

ApprovalTargetType = Literal[
    "edit-plan",
    "presentation-bundle",
    "privacy-report",
    "rights-report",
    "final-render",
    "publication-bundle",
    "frozen-timeline",
]

PURPOSE_TARGET_TYPES: Final[dict[ApprovalPurpose, frozenset[str]]] = {
    "editorial": frozenset({"edit-plan"}),
    "presentation": frozenset({"presentation-bundle"}),
    "privacy": frozenset({"privacy-report"}),
    "rights": frozenset({"rights-report"}),
    "final": frozenset({"final-render"}),
    "publication": frozenset({"publication-bundle"}),
    "manual_freeze": frozenset({"frozen-timeline"}),
}

RunnerClass = Literal["operator", "automation"]
RecordDecision = Literal["approve", "reject"]


class OperationDraft(StrictModel):
    """Pre-publication form; the store assigns id/seq/supersession."""

    purpose: ApprovalPurpose
    target_type: ApprovalTargetType
    target_hash: Sha256
    decision: RecordDecision
    actor_id: Identifier
    uid: int | None = Field(default=None, ge=0, strict=True)
    tty: str | None = None
    wall_time_unix: int | None = Field(default=None, ge=0, strict=True)
    fixture_only: bool
    runner_class: RunnerClass
    final_binding: FinalReviewBinding | None = None

    @model_validator(mode="after")
    def enforce_class_rules(self) -> OperationDraft:
        if self.runner_class == "automation" and not self.fixture_only:
            raise PydanticCustomError(
                "automation_not_fixture",
                "automation-class runners can never produce fixture_only=false records",
            )
        if not self.fixture_only and (self.uid is None or not self.tty):
            raise PydanticCustomError(
                "operator_uid_tty_missing",
                "operator records require the uid and controlling tty captured at ingress",
            )
        if self.target_type not in PURPOSE_TARGET_TYPES[self.purpose]:
            raise PydanticCustomError(
                "purpose_target_mismatch",
                "purpose {purpose} cannot bind target type {target}",
                {"purpose": self.purpose, "target": self.target_type},
            )
        if self.final_binding is not None and self.purpose != "final":
            raise PydanticCustomError(
                "final_binding_presence",
                "only purpose 'final' may carry a final_review binding",
            )
        return self


    @property
    def supersession_key(self) -> tuple[str, str]:
        return (self.purpose, self.target_hash)


class OperationRecord(OperationDraft):
    record_id: Identifier
    timestamp_seq: PositiveInteger
    superseded_record_id: Identifier | None = None

    @model_validator(mode="after")
    def reject_self_supersession(self) -> OperationRecord:
        if self.superseded_record_id == self.record_id:
            raise PydanticCustomError(
                "self_supersession", "a record cannot supersede itself"
            )
        return self


GENESIS_RECORD_HASH: str = "0" * 64

RECORD_ID_PREFIX: Final = "opr"


def record_id_for_seq(timestamp_seq: int) -> Identifier:
    return f"{RECORD_ID_PREFIX}-{timestamp_seq:08d}"


class ChainedOperationRecord(OperationRecord):
    """One append-only log line: record plus keyed-hash-chain fields.

    ``record_hash`` is HMAC-SHA256(chain_key, canonical bytes with the
    hash field zeroed) — the key is the store-local secret from
    ``services.approvals.chain_key``; an offline unkeyed recompute can
    never produce a valid seal.
    """

    previous_record_hash: Sha256
    record_hash: Sha256

    def recomputed_record_hash(self, *, chain_key: bytes) -> str:
        payload = self.model_copy(update={"record_hash": GENESIS_RECORD_HASH})
        return hmac.new(
            chain_key, canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
