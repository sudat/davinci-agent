"""FinalReviewLedger: append-only events, approval views, invalidation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.final_review.ledger import FinalReviewLedger, LedgerIntegrityError
from tests.final_review.support import sha


def make_ledger(tmp_path) -> FinalReviewLedger:
    return FinalReviewLedger(tmp_path / "final-review-events.jsonl")


def test_bind_bundle_records_active_target(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    target = sha("target-a")
    ledger.bind_bundle(target)
    assert ledger.active_bundle_hash() == target


def test_approval_recorded_and_invalidated(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    target = sha("target-a")
    ledger.bind_bundle(target)
    ledger.record_final_approval("opr-00000002", target)
    approval = ledger.active_final_approval()
    assert approval is not None
    assert approval.record_id == "opr-00000002"
    ledger.invalidate_approvals(code="correction-cycle", detail="new plan v2")
    assert ledger.active_final_approval() is None
    assert "opr-00000002" in ledger.approval_invalidations()


def test_invalidation_records_reason_and_target(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    target = sha("target-a")
    ledger.bind_bundle(target)
    ledger.record_final_approval("opr-00000003", target)
    invalidated = ledger.invalidate_approvals(code="bundle-changed", detail="rebuild")
    assert len(invalidated) == 1
    event = ledger.events()[-1]
    assert event.event == "approval-invalidated"
    assert event.detail.startswith("bundle-changed")


def test_tampered_event_line_refused(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.bind_bundle(sha("target-a"))
    path = tmp_path / "final-review-events.jsonl"
    raw = path.read_bytes().replace(b"bundle-bound", b"bundle-tampered")
    path.write_bytes(raw)
    with pytest.raises((LedgerIntegrityError, ValidationError)):
        ledger.events()


def test_rebinding_new_bundle_leaves_history_append_only(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    first = sha("target-a")
    second = sha("target-b")
    ledger.bind_bundle(first)
    ledger.record_final_approval("opr-00000002", first)
    ledger.bind_bundle(second)
    assert ledger.active_bundle_hash() == second
    assert [event.event for event in ledger.events()] == [
        "bundle-bound",
        "approval-recorded",
        "approval-invalidated",
        "bundle-bound",
    ]
    assert ledger.active_final_approval() is None
    assert "opr-00000002" in ledger.approval_invalidations()
