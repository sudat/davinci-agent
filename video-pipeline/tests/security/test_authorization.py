"""Attack class 5: unauthorized command/approval — denied, nothing written.

Active attempts against the real surfaces: the operator CLI dispatch
(refusal classes must never spawn a process), the TTY approval ingress
(automation class, non-TTY descriptors, aborted confirmation), and the
authorization verifier (fixture-marked / superseded / purpose-mismatched
records never authorize an operator gate). Every attempt snapshots the
target area and proves zero records were written.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from services.approvals.ingress import IngressRefusalError, record_operation
from services.approvals.verify import (
    REFUSAL_FIXTURE,
    evaluate_authorization,
    validate_supersession_chain,
)
from services.cli import operator
from services.cli.operator import OperatorRefusalError, dispatch, refuse
from tests.approvals.support import TARGET_A, hand_chained
from tests.security.support import assert_zero_side_effects, snapshot_tree

REFUSAL_NAMES = (
    ("sh", operator.REFUSAL_SHELL),
    ("bash", operator.REFUSAL_SHELL),
    ("curl", operator.REFUSAL_NETWORK),
    ("wget", operator.REFUSAL_NETWORK),
    ("cat", operator.REFUSAL_PATH),
    ("rm", operator.REFUSAL_PATH),
    ("ui", operator.REFUSAL_UI),
    ("serve", operator.REFUSAL_UI),
    ("definitely-not-an-operation", operator.REFUSAL_UNKNOWN),
)


def test_10_every_refusal_class_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned: list[object] = []

    def no_spawn(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned.append(_args)
        raise AssertionError("a refusal must never execute anything")

    monkeypatch.setattr(operator.subprocess, "run", no_spawn)
    before = snapshot_tree(tmp_path)
    for name, code in REFUSAL_NAMES:
        error = refuse(name)
        assert isinstance(error, OperatorRefusalError)
        assert error.code == code
        assert dispatch([name, "--host", "0.0.0.0"]) == 2  # noqa: S104 (attack payload)
    assert spawned == []
    assert_zero_side_effects(tmp_path, before)


def test_11_registered_operation_dispatches_explicit_argv_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(operator.subprocess, "run", fake_run)
    assert dispatch(["check-scope", "--phase", "3"]) == 0
    assert captured == [[sys.executable, "-m", "services.policy.check_scope", "--phase", "3"]]


def test_20_automation_class_refused_even_on_a_real_tty(tmp_path: Path) -> None:
    store_dir = tmp_path / "records"
    store_dir.mkdir()
    before = snapshot_tree(store_dir)
    master, slave = pty.openpty()
    try:
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id="attacker-automation",
                tty_fd=slave,
                runner_class="automation",
            )
        assert error.value.code == "automation-refused"
    finally:
        os.close(master)
        os.close(slave)
    assert_zero_side_effects(store_dir, before)


def test_21_non_tty_descriptors_refused(tmp_path: Path) -> None:
    store_dir = tmp_path / "records"
    store_dir.mkdir()
    before = snapshot_tree(store_dir)
    devnull = os.open(os.devnull, os.O_RDONLY)
    read_end, write_end = os.pipe()
    try:
        for fd in (devnull, read_end):
            with pytest.raises(IngressRefusalError) as error:
                record_operation(
                    purpose="editorial",
                    target_bundle_hash=TARGET_A,
                    decision="approve",
                    actor_id="attacker-notty",
                    tty_fd=fd,
                )
            assert error.value.code == "not-a-tty"
    finally:
        os.close(devnull)
        os.close(read_end)
        os.close(write_end)
    assert_zero_side_effects(store_dir, before)


def test_22_wrong_confirmation_writes_nothing(tmp_path: Path) -> None:
    store_dir = tmp_path / "records"
    store_dir.mkdir()
    before = snapshot_tree(store_dir)
    master, slave = pty.openpty()
    try:
        os.write(master, b"yes-please-approve-everything\n")
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id="attacker-typos",
                tty_fd=slave,
            )
        assert error.value.code == "operator-aborted"
    finally:
        os.close(master)
        os.close(slave)
    assert_zero_side_effects(store_dir, before)


def test_23_malformed_target_hash_refused() -> None:
    with pytest.raises(IngressRefusalError) as error:
        record_operation(
            purpose="editorial",
            target_bundle_hash="not-a-sha",
            decision="approve",
            actor_id="attacker",
            tty_fd=None,
        )
    assert error.value.code == "invalid-target-hash"


def chain_hash(seq: int) -> str:
    return hand_chained(seq=seq).record_hash


def test_30_fixture_marked_record_never_authorizes_operator_gate() -> None:
    chain = (hand_chained(seq=1), hand_chained(seq=2, previous_hash=chain_hash(1)))
    verdict = evaluate_authorization(
        chain,
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=True,
    )
    assert verdict.authorized is False
    assert verdict.refusal_code == REFUSAL_FIXTURE


def test_31_tampered_record_chain_never_authorizes() -> None:
    forged = hand_chained(seq=1, tamper=True)
    with pytest.raises(Exception):  # noqa: B017, PT011 (typed VerificationError)
        validate_supersession_chain((forged,))
    verdict = evaluate_authorization(
        (forged,),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=True,
    )
    assert verdict.authorized is False
