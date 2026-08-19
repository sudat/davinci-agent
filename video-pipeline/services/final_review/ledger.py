"""Append-only final-review event ledger with decision invalidation views.

The ledger records bundle bindings, correction cycles, FINAL approvals,
approval invalidations, and freeze routing. Every line is a sealed
``FinalReviewEvent``; reads re-validate sequence and content hashes, so
reordering, editing, or deleting lines is a hard error. Superseded FINAL
records are recorded as ``approval-invalidated`` events — never deleted —
and ``active_final_approval`` only reflects approvals not superseded by a
later bundle or correction cycle.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from services.contracts.primitives import PositiveInteger, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes

FinalReviewEventKind = Literal[
    "bundle-bound",
    "correction-cycle",
    "approval-recorded",
    "approval-invalidated",
    "freeze-routed",
]


class LedgerIntegrityError(Exception):
    """The event ledger failed sequence/hash validation."""


class FinalReviewEvent(StrictModel):
    event_seq: PositiveInteger
    event: FinalReviewEventKind
    detail: str = Field(min_length=1, strict=True)
    target_set_hash: Sha256 | None = None
    record_id: str | None = None
    event_hash: Sha256

    def computed_event_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"event_hash": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()


class ApprovalBinding(StrictModel):
    record_id: str
    target_set_hash: Sha256


class FinalReviewLedger:
    def __init__(self, events_path: Path) -> None:
        self._path = events_path
        events_path.parent.mkdir(parents=True, exist_ok=True)

    def events(self) -> tuple[FinalReviewEvent, ...]:
        return self._read_validated()

    def verify_chain(self) -> None:
        self._read_validated()

    def bind_bundle(self, target_set_hash: str) -> FinalReviewEvent:
        active = self.active_bundle_hash()
        if active is not None and active != target_set_hash:
            self.invalidate_approvals(
                code="bundle-changed",
                detail=f"new bundle target {target_set_hash} supersedes {active}",
            )
        return self._append(
            "bundle-bound", f"bundle displayed target {target_set_hash}", target_set_hash
        )

    def record_final_approval(
        self, record_id: str, target_set_hash: str
    ) -> FinalReviewEvent:
        return self._append(
            "approval-recorded",
            f"FINAL_APPROVED record {record_id} vs {target_set_hash}",
            target_set_hash,
            record_id=record_id,
        )

    def record_correction_cycle(
        self, instruction: str, target_set_hash: str | None
    ) -> FinalReviewEvent:
        return self._append(
            "correction-cycle", f"correction: {instruction}", target_set_hash
        )

    def record_freeze_routed(
        self, package_sha256: str, target_set_hash: str | None
    ) -> FinalReviewEvent:
        return self._append(
            "freeze-routed",
            f"freeze package {package_sha256}",
            target_set_hash,
        )

    def invalidate_approvals(self, *, code: str, detail: str) -> tuple[str, ...]:
        invalidated = self._invalidated_ids()
        superseded = [
            binding.record_id
            for binding in self._approval_bindings()
            if binding.record_id not in invalidated
        ]
        for record_id in superseded:
            self._append(
                "approval-invalidated",
                f"{code}: {detail}",
                self.active_bundle_hash(),
                record_id=record_id,
            )
        return tuple(superseded)

    def active_bundle_hash(self) -> Sha256 | None:
        bound = [event for event in self.events() if event.event == "bundle-bound"]
        return bound[-1].target_set_hash if bound else None

    def active_final_approval(self) -> ApprovalBinding | None:
        live = [
            binding
            for binding in self._approval_bindings()
            if binding.record_id not in self._invalidated_ids()
        ]
        return live[-1] if live else None

    def approval_invalidations(self) -> frozenset[str]:
        return frozenset(self._invalidated_ids())

    def _approval_bindings(self) -> tuple[ApprovalBinding, ...]:
        return tuple(
            ApprovalBinding(
                record_id=event.record_id or "",
                target_set_hash=event.target_set_hash or GENESIS_SHA256,
            )
            for event in self.events()
            if event.event == "approval-recorded"
        )

    def _invalidated_ids(self) -> tuple[str, ...]:
        return tuple(
            event.record_id or ""
            for event in self.events()
            if event.event == "approval-invalidated"
        )

    def _read_validated(self) -> tuple[FinalReviewEvent, ...]:
        if not self._path.exists():
            return ()
        try:
            events = [
                FinalReviewEvent.model_validate_json(line)
                for line in self._path.read_bytes().splitlines()
                if line.strip()
            ]
        except ValidationError as error:
            raise LedgerIntegrityError(f"event-line-invalid: {error}") from error
        for position, event in enumerate(events, start=1):
            if event.event_seq != position:
                raise LedgerIntegrityError(
                    f"sequence-broken: expected {position}, found {event.event_seq}"
                )
            if event.computed_event_hash() != event.event_hash:
                raise LedgerIntegrityError(
                    f"event-hash-tampered: event {event.event_seq}"
                )
        return tuple(events)

    def _append(
        self,
        kind: FinalReviewEventKind,
        detail: str,
        target_set_hash: str | None,
        *,
        record_id: str | None = None,
    ) -> FinalReviewEvent:
        events = self._read_validated()
        seq = len(events) + 1
        unsealed = FinalReviewEvent(
            event_seq=seq,
            event=kind,
            detail=detail,
            target_set_hash=target_set_hash,
            record_id=record_id,
            event_hash=GENESIS_SHA256,
        )
        sealed = unsealed.model_copy(
            update={"event_hash": unsealed.computed_event_hash()}
        )
        descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, canonical_json_bytes(sealed) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return sealed


__all__ = [
    "ApprovalBinding",
    "FinalReviewEvent",
    "FinalReviewEventKind",
    "FinalReviewLedger",
    "LedgerIntegrityError",
]
