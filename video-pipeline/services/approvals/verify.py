"""Verification and authorization semantics for operation records.

Purpose/target separation: each purpose binds to exactly one target
type (registry in ``models``); an Editorial record can never authorize
a Presentation target. Fixture-vs-operator labeling: fixture-marked
records never satisfy operator gates. Supersession: only the latest
non-superseded record per (purpose, target) may authorize, and the
supersession chain must be linear and complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from services.approvals.models import (
    GENESIS_RECORD_HASH,
    PURPOSE_TARGET_TYPES,
    ChainedOperationRecord,
    OperationRecord,
    record_id_for_seq,
)

REFUSAL_NO_RECORD: Final = "no-record"
REFUSAL_SUPERSEDED: Final = "superseded-record"
REFUSAL_PURPOSE_TARGET: Final = "purpose-target-mismatch"
REFUSAL_DECISION: Final = "decision-reject"
REFUSAL_FIXTURE: Final = "fixture-record"


class VerificationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AuthorizationVerdict:
    authorized: bool
    refusal_code: str = ""
    record_id: str | None = None
    purpose: str = ""
    target_hash: str = ""


def validate_operation_record(record: OperationRecord) -> None:
    """Re-check structural invariants for records from any source."""

    if record.target_type not in PURPOSE_TARGET_TYPES[record.purpose]:
        raise VerificationError(
            "purpose-target-mismatch",
            f"purpose {record.purpose} cannot bind target type {record.target_type}",
        )
    if record.runner_class == "automation" and not record.fixture_only:
        raise VerificationError(
            "automation-not-fixture",
            "automation-class runners can never produce fixture_only=false records",
        )
    if not record.fixture_only and (record.uid is None or not record.tty):
        raise VerificationError(
            "operator-uid-tty-missing",
            "operator records require uid and controlling tty captured at ingress",
        )


def _check_chain_links(
    record: ChainedOperationRecord,
    expected_seq: int,
    previous_hash: str,
    by_id: dict[str, ChainedOperationRecord],
    *,
    chain_key: bytes,
) -> None:
    if record.timestamp_seq != expected_seq:
        raise VerificationError(
            "sequence-broken",
            f"expected seq {expected_seq}, found {record.timestamp_seq}"
            f" (reordered/deleted lines)",
        )
    if record.record_id != record_id_for_seq(record.timestamp_seq):
        raise VerificationError(
            "record-id-mismatch",
            f"record id {record.record_id} does not match its sequence",
        )
    if record.record_id in by_id:
        raise VerificationError("duplicate-record", record.record_id)
    if record.previous_record_hash != previous_hash:
        raise VerificationError(
            "chain-broken",
            f"record {record.record_id} does not link to the previous hash",
        )
    if record.recomputed_record_hash(chain_key=chain_key) != record.record_hash:
        raise VerificationError(
            "record-hash-tampered",
            f"record {record.record_id} content does not hash to its seal "
            "(keyed HMAC; offline-forged chains fail here)",
        )


def _check_supersession_link(
    record: ChainedOperationRecord,
    by_id: dict[str, ChainedOperationRecord],
    superseding: dict[str, str],
) -> None:
    target = record.superseded_record_id
    if target is None:
        return
    if target not in by_id:
        raise VerificationError(
            "supersession-dangling",
            f"record {record.record_id} supersedes unknown {target}",
        )
    if target in superseding:
        raise VerificationError(
            "supersession-conflict",
            f"record {target} is superseded by more than one record",
        )
    if by_id[target].supersession_key != record.supersession_key:
        raise VerificationError(
            "supersession-key-mismatch",
            f"record {record.record_id} supersedes across purpose/target",
        )
    superseding[target] = record.record_id


def validate_supersession_chain(
    records: tuple[ChainedOperationRecord, ...],
    *,
    chain_key: bytes,
) -> None:
    """Fail closed on keyed hash-chain breaks or supersession inconsistencies.

    ``chain_key`` is REQUIRED: every seal is an HMAC under the store-local
    key (see ``services.approvals.chain_key``), so a chain forged offline
    without the key fails ``record-hash-tampered``.
    """

    superseding: dict[str, str] = {}
    by_id: dict[str, ChainedOperationRecord] = {}
    previous_hash = GENESIS_RECORD_HASH
    expected_seq = 1
    for record in records:
        _check_chain_links(
            record, expected_seq, previous_hash, by_id, chain_key=chain_key
        )
        _check_supersession_link(record, by_id, superseding)
        validate_operation_record(record)
        by_id[record.record_id] = record
        previous_hash = record.record_hash
        expected_seq += 1


def evaluate_authorization(
    records: tuple[ChainedOperationRecord, ...],
    *,
    purpose: str,
    target_hash: str,
    target_type: str,
    operator_gate: bool,
) -> AuthorizationVerdict:
    """Latest-wins authorization with purpose/target separation.

    A superseded record never authorizes; a fixture-marked record never
    satisfies an operator gate; a record whose purpose/target binding
    differs from the request is refused with ``purpose-target-mismatch``
    (an Editorial approval cannot authorize a Presentation target).
    """

    superseded_ids = {record.superseded_record_id for record in records}
    any_for_target = [r for r in records if r.target_hash == target_hash]
    latest = [
        r
        for r in any_for_target
        if r.record_id not in superseded_ids and r.purpose == purpose
    ]
    if not latest:
        if any_for_target:
            return AuthorizationVerdict(
                authorized=False,
                refusal_code=REFUSAL_PURPOSE_TARGET
                if any(r.purpose != purpose for r in any_for_target)
                else REFUSAL_SUPERSEDED,
                record_id=any_for_target[-1].record_id,
                purpose=purpose,
                target_hash=target_hash,
            )
        return AuthorizationVerdict(
            authorized=False,
            refusal_code=REFUSAL_NO_RECORD,
            purpose=purpose,
            target_hash=target_hash,
        )
    record = latest[-1]
    if record.target_type != target_type or record.purpose != purpose:
        return AuthorizationVerdict(
            authorized=False,
            refusal_code=REFUSAL_PURPOSE_TARGET,
            record_id=record.record_id,
            purpose=purpose,
            target_hash=target_hash,
        )
    if operator_gate and record.fixture_only:
        return AuthorizationVerdict(
            authorized=False,
            refusal_code=REFUSAL_FIXTURE,
            record_id=record.record_id,
            purpose=purpose,
            target_hash=target_hash,
        )
    if record.decision == "reject":
        return AuthorizationVerdict(
            authorized=False,
            refusal_code=REFUSAL_DECISION,
            record_id=record.record_id,
            purpose=purpose,
            target_hash=target_hash,
        )
    return AuthorizationVerdict(
        authorized=True,
        record_id=record.record_id,
        purpose=purpose,
        target_hash=target_hash,
    )


__all__ = [
    "AuthorizationVerdict",
    "VerificationError",
    "evaluate_authorization",
    "validate_operation_record",
    "validate_supersession_chain",
]
