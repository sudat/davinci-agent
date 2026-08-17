"""Append-only, hash-chained outbound request audit (PRD 27).

Every record passes through redaction before it is stored: free-text fields
(``endpoint_declared``, ``policy_reason``) are masked by ``redact_text`` and
all remaining fields are strict literals, sha256 values, or integers, so no
secret or credential is ever written. The JSONL file chains records by sha256
with the record hash zeroed (the established ledger pattern); reads re-verify
the whole chain and fail closed on drift. Chain verification detects mutation
of the retained file only — unrecorded requests and owner-level rewrites are
explicit trust boundaries, not claimed detections.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from services.config.models import DataClass  # noqa: TC001 (pydantic resolves this at model build)
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.policy.redaction import redact_text

GENESIS_HASH = "0" * 64
OutboundDestinationClass = Literal["cloud", "local_tool"]
OutboundPolicyDecision = Literal["allow", "deny"]


class OutboundAuditError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class OutboundRequestInput(StrictModel):
    destination_class: OutboundDestinationClass
    endpoint_declared: str = Field(min_length=1)
    data_classes: tuple[DataClass, ...] = Field(min_length=1)
    policy_decision: OutboundPolicyDecision
    policy_reason: str = Field(min_length=1)
    request_sha256: Sha256
    timestamp_seq: int = Field(ge=1, strict=True)


class OutboundRequestRecord(StrictModel):
    sequence: int = Field(ge=1, strict=True)
    previous_event_hash: Sha256
    event_hash: Sha256
    destination_class: OutboundDestinationClass
    endpoint_declared: str
    data_classes: tuple[DataClass, ...]
    policy_decision: OutboundPolicyDecision
    policy_reason: str
    request_sha256: Sha256
    timestamp_seq: int = Field(ge=1, strict=True)


def _record_hash(record: OutboundRequestRecord) -> str:
    payload = record.model_copy(update={"event_hash": GENESIS_HASH})
    return hashlib.sha256(canonical_model_bytes(payload)).hexdigest()


class OutboundRequestAudit:
    """Hash-chained JSONL audit of outbound data requests, one file per job."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def records(self) -> tuple[OutboundRequestRecord, ...]:
        """Read and chain-verify every record; fail closed on any drift."""

        if not self._path.exists():
            return ()
        records: list[OutboundRequestRecord] = []
        previous = GENESIS_HASH
        for line in self._path.read_bytes().splitlines():
            try:
                record = OutboundRequestRecord.model_validate_json(line)
            except ValidationError as error:
                raise OutboundAuditError("audit-corrupt", str(error)) from error
            if record.previous_event_hash != previous or record.event_hash != _record_hash(record):
                raise OutboundAuditError(
                    "audit-chain", f"record {record.sequence} breaks the audit hash chain"
                )
            records.append(record)
            previous = record.event_hash
        return tuple(records)

    def record_request(self, request: OutboundRequestInput) -> OutboundRequestRecord:
        """Redact, chain, and append one request record; returns the record."""

        existing = self.records()
        if existing and request.timestamp_seq <= existing[-1].timestamp_seq:
            raise OutboundAuditError(
                "audit-timestamp",
                f"logical timestamp_seq {request.timestamp_seq} does not advance past "
                f"{existing[-1].timestamp_seq}",
            )
        record = OutboundRequestRecord(
            sequence=(existing[-1].sequence + 1) if existing else 1,
            previous_event_hash=existing[-1].event_hash if existing else GENESIS_HASH,
            event_hash=GENESIS_HASH,
            destination_class=request.destination_class,
            endpoint_declared=redact_text(request.endpoint_declared),
            data_classes=request.data_classes,
            policy_decision=request.policy_decision,
            policy_reason=redact_text(request.policy_reason),
            request_sha256=request.request_sha256,
            timestamp_seq=request.timestamp_seq,
        )
        chained = record.model_copy(update={"event_hash": _record_hash(record)})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as stream:
            stream.write(canonical_model_bytes(chained) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return chained

    def audit_query(
        self,
        *,
        destination_class: OutboundDestinationClass | None = None,
        policy_decision: OutboundPolicyDecision | None = None,
    ) -> tuple[OutboundRequestRecord, ...]:
        """Chain-verified records, optionally filtered for verification."""

        return tuple(
            record
            for record in self.records()
            if (destination_class is None or record.destination_class == destination_class)
            and (policy_decision is None or record.policy_decision == policy_decision)
        )


__all__ = [
    "GENESIS_HASH",
    "OutboundAuditError",
    "OutboundDestinationClass",
    "OutboundPolicyDecision",
    "OutboundRequestAudit",
    "OutboundRequestInput",
    "OutboundRequestRecord",
]
