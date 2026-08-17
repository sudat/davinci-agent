from __future__ import annotations

import os
import pty

import pytest

from services.approvals.ingress import (
    IngressRefusalError,
    record_fixture_operation,
    record_operation,
)
from tests.approvals.support import ACTOR, TARGET_A


def ingress_over_pty(
    *,
    confirm: bytes,
    runner_class: str = "operator",
    fixture: bool = False,
    purpose: str = "editorial",
):
    master, slave = pty.openpty()
    try:
        os.write(master, confirm)
        return record_operation(
            purpose=purpose,  # type: ignore[arg-type]
            target_bundle_hash=TARGET_A,
            decision="approve",
            actor_id=ACTOR,
            tty_fd=slave,
            runner_class=runner_class,  # type: ignore[arg-type]
            fixture=fixture,
        )
    finally:
        os.close(master)
        os.close(slave)


def test_operator_ingress_via_real_pty_records_uid_and_tty() -> None:
    draft = ingress_over_pty(confirm=b"confirm\n")
    assert draft.fixture_only is False
    assert draft.uid == os.getuid()
    assert draft.tty is not None
    assert draft.tty.startswith("/")
    assert draft.runner_class == "operator"


def test_operator_ingress_refuses_wrong_confirmation() -> None:
    with pytest.raises(IngressRefusalError) as error:
        ingress_over_pty(confirm=b"nope\n")
    assert error.value.code == "operator-aborted"


def test_operator_ingress_requires_real_tty() -> None:
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=devnull,
            )
        assert error.value.code == "not-a-tty"
    finally:
        os.close(devnull)


def test_operator_ingress_refuses_pipe_fd() -> None:
    read_end, _write_end = os.pipe()
    try:
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=read_end,
            )
        assert error.value.code == "not-a-tty"
    finally:
        os.close(read_end)
        os.close(_write_end)


def test_automation_runner_class_refused_even_on_real_tty() -> None:
    master, slave = pty.openpty()
    try:
        os.write(master, b"confirm\n")
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=slave,
                runner_class="automation",
            )
        assert error.value.code == "automation-refused"
    finally:
        os.close(master)
        os.close(slave)


def test_missing_tty_fd_refused() -> None:
    with pytest.raises(IngressRefusalError) as error:
        record_operation(
            purpose="editorial",
            target_bundle_hash=TARGET_A,
            decision="approve",
            actor_id=ACTOR,
            tty_fd=None,
        )
    assert error.value.code == "not-a-tty"


def test_tty_fixture_mode_keeps_record_fixture_marked() -> None:
    draft = ingress_over_pty(confirm=b"confirm\n", fixture=True)
    assert draft.fixture_only is True


def test_fixture_seam_creates_fixture_marked_records() -> None:
    draft = record_fixture_operation(
        purpose="editorial",
        target_bundle_hash=TARGET_A,
        decision="approve",
        actor_id=ACTOR,
    )
    assert draft.fixture_only is True
    assert draft.runner_class == "automation"


def test_fixture_seam_never_produces_operator_records() -> None:
    drafts = [
        record_fixture_operation(
            purpose=purpose,
            target_bundle_hash=TARGET_A,
            decision="approve",
            actor_id=ACTOR,
        )
        for purpose in (
            "editorial",
            "presentation",
            "privacy",
            "rights",
            "final",
            "publication",
            "manual_freeze",
        )
    ]
    assert all(draft.fixture_only for draft in drafts)


def test_invalid_target_hash_refused() -> None:
    with pytest.raises(IngressRefusalError) as error:
        record_fixture_operation(
            purpose="editorial",
            target_bundle_hash="not-a-hash",
            decision="approve",
            actor_id=ACTOR,
        )
    assert error.value.code == "invalid-target-hash"
