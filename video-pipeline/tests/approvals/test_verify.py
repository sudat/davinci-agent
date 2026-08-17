from __future__ import annotations

from pathlib import Path

import pytest

from services.approvals.store import GENESIS_RECORD_HASH
from services.approvals.verify import (
    VerificationError,
    evaluate_authorization,
    validate_operation_record,
    validate_supersession_chain,
)
from tests.approvals.support import TARGET_A, TARGET_B, fixture_draft, hand_chained, make_store


def test_fixture_record_authorizes_automated_gate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = store.append(fixture_draft(decision="approve"))
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=False,
    )
    assert verdict.authorized is True
    assert verdict.record_id == record.record_id


def test_superseded_record_no_longer_authorizes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.append(fixture_draft(decision="approve"))
    second = store.append(fixture_draft(decision="reject"))
    assert second.superseded_record_id == first.record_id
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=False,
    )
    assert verdict.authorized is False
    assert verdict.refusal_code == "decision-reject"
    assert verdict.record_id == second.record_id


def test_editorial_record_cannot_authorize_presentation_target(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft(purpose="editorial", decision="approve"))
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="presentation",
        target_hash=TARGET_A,
        target_type="presentation-bundle",
        operator_gate=False,
    )
    assert verdict.authorized is False
    assert verdict.refusal_code == "purpose-target-mismatch"


def test_fixture_record_never_satisfies_operator_gate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft(decision="approve"))
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=True,
    )
    assert verdict.authorized is False
    assert verdict.refusal_code == "fixture-record"


def test_no_record_for_target_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=TARGET_B,
        target_type="edit-plan",
        operator_gate=False,
    )
    assert verdict.authorized is False
    assert verdict.refusal_code == "no-record"


def test_latest_decision_wins_after_three_records(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft(decision="reject"))
    store.append(fixture_draft(decision="approve"))
    third = store.append(fixture_draft(decision="reject"))
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=False,
    )
    assert verdict.authorized is False
    assert verdict.record_id == third.record_id


def test_validate_operation_record_accepts_store_records(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = store.append(fixture_draft())
    validate_operation_record(record)


def test_chain_broken_detected_on_hand_built_records() -> None:
    first = hand_chained(seq=1)
    second = hand_chained(seq=2, previous_hash="e" * 64, superseded=first.record_id)
    with pytest.raises(VerificationError) as error:
        validate_supersession_chain((first, second))
    assert error.value.code == "chain-broken"


def test_record_hash_tamper_detected() -> None:
    with pytest.raises(VerificationError) as error:
        validate_supersession_chain((hand_chained(seq=1, tamper=True),))
    assert error.value.code == "record-hash-tampered"


def test_dangling_supersession_detected() -> None:
    with pytest.raises(VerificationError) as error:
        validate_supersession_chain(
            (hand_chained(seq=1, superseded="opr-00000009"),)
        )
    assert error.value.code == "supersession-dangling"


def test_conflicting_supersession_detected() -> None:
    first = hand_chained(seq=1)
    second = hand_chained(seq=2, previous_hash=first.record_hash, superseded=first.record_id)
    third = hand_chained(
        seq=3, previous_hash=second.record_hash, superseded=first.record_id
    )
    with pytest.raises(VerificationError) as error:
        validate_supersession_chain((first, second, third))
    assert error.value.code == "supersession-conflict"


def test_cross_target_supersession_detected() -> None:
    first = hand_chained(seq=1, target=TARGET_A)
    second = hand_chained(
        seq=2,
        previous_hash=first.record_hash,
        target=TARGET_B,
        superseded=first.record_id,
    )
    with pytest.raises(VerificationError) as error:
        validate_supersession_chain((first, second))
    assert error.value.code == "supersession-key-mismatch"


def test_genesis_previous_hash_required_for_first_record() -> None:
    with pytest.raises(VerificationError):
        validate_supersession_chain((hand_chained(seq=1, previous_hash="a" * 64),))


def test_empty_chain_valid() -> None:
    validate_supersession_chain(())
    assert GENESIS_RECORD_HASH == "0" * 64
