from __future__ import annotations

import os
import pty

import pytest
from pydantic import ValidationError

from services.approvals.models import (
    PURPOSE_TARGET_TYPES,
    OperationDraft,
    OperationRecord,
)
from tests.approvals.support import ACTOR, TARGET_A


def operator_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "purpose": "editorial",
        "target_type": "edit-plan",
        "target_hash": TARGET_A,
        "decision": "approve",
        "actor_id": ACTOR,
        "uid": os.getuid(),
        "tty": "/dev/ttys000",
        "wall_time_unix": None,
        "fixture_only": False,
        "runner_class": "operator",
        "record_id": "opr-00000001",
        "timestamp_seq": 1,
        "superseded_record_id": None,
    }
    payload.update(overrides)
    return payload


def test_valid_operator_record_parses() -> None:
    record = OperationRecord.model_validate(operator_payload())
    assert record.fixture_only is False
    assert record.supersession_key == ("editorial", TARGET_A)


def test_all_seven_purposes_bind_distinct_target_types() -> None:
    assert len(PURPOSE_TARGET_TYPES) == 7
    bound = {next(iter(types)) for types in PURPOSE_TARGET_TYPES.values()}
    assert len(bound) == 7


def test_purpose_target_binding_rejects_wrong_target_type() -> None:
    with pytest.raises(ValidationError, match="purpose_target_mismatch"):
        OperationRecord.model_validate(
            operator_payload(target_type="presentation-bundle")
        )


def test_automation_cannot_produce_operator_records() -> None:
    with pytest.raises(ValidationError, match="automation_not_fixture"):
        OperationRecord.model_validate(
            operator_payload(runner_class="automation")
        )


def test_operator_record_requires_uid_and_tty() -> None:
    with pytest.raises(ValidationError, match="operator_uid_tty_missing"):
        OperationRecord.model_validate(operator_payload(uid=None))
    with pytest.raises(ValidationError, match="operator_uid_tty_missing"):
        OperationRecord.model_validate(operator_payload(tty=None))


def test_fixture_record_allows_missing_uid_and_tty() -> None:
    draft = OperationDraft.model_validate(
        {
            "purpose": "editorial",
            "target_type": "edit-plan",
            "target_hash": TARGET_A,
            "decision": "approve",
            "actor_id": ACTOR,
            "fixture_only": True,
            "runner_class": "automation",
        }
    )
    assert draft.uid is None
    assert draft.tty is None


def test_floats_rejected_everywhere() -> None:
    with pytest.raises(ValidationError):
        OperationRecord.model_validate(operator_payload(uid=1.5))
    with pytest.raises(ValidationError):
        OperationRecord.model_validate(operator_payload(wall_time_unix=1.25))
    with pytest.raises(ValidationError):
        OperationRecord.model_validate(operator_payload(timestamp_seq=2.0))


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        OperationRecord.model_validate(operator_payload(forged="true"))


def test_wall_time_is_optional_non_canonical_metadata() -> None:
    with_time = OperationRecord.model_validate(operator_payload(wall_time_unix=10**9))
    without_time = OperationRecord.model_validate(operator_payload())
    assert with_time.wall_time_unix == 10**9
    assert without_time.wall_time_unix is None


def test_unknown_purpose_rejected() -> None:
    with pytest.raises(ValidationError):
        OperationRecord.model_validate(operator_payload(purpose="supervisor"))


def test_record_cannot_supersede_itself() -> None:
    with pytest.raises(ValidationError, match="self_supersession"):
        OperationRecord.model_validate(
            operator_payload(superseded_record_id="opr-00000001")
        )


def test_pty_module_available_for_real_tty_tests() -> None:
    master, slave = pty.openpty()
    try:
        assert os.isatty(slave)
    finally:
        os.close(master)
        os.close(slave)
