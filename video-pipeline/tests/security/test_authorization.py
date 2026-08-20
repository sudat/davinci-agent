"""Attack class 5: unauthorized command/approval — denied, nothing written.

Active attempts against the real surfaces: the operator CLI dispatch
(refusal classes must never spawn a process), the TTY approval ingress
(automation class, non-TTY descriptors, aborted confirmation), and the
authorization verifier (fixture-marked / superseded / purpose-mismatched
records never authorize an operator gate). Every attempt snapshots the
target area and proves zero records were written.
"""

from __future__ import annotations

import hashlib
import os
import pty
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from services.approvals.chain_key import CHAIN_KEY_ENV, ChainKeyError, load_chain_key
from services.approvals.ingress import IngressRefusalError, record_operation
from services.approvals.models import ChainedOperationRecord
from services.approvals.store import OperationRecordError, OperationRecordStore
from services.approvals.verify import (
    REFUSAL_FIXTURE,
    evaluate_authorization,
    validate_supersession_chain,
)
from services.cli import operator
from services.cli.operator import OperatorRefusalError, dispatch, refuse
from services.contracts.serialization import canonical_json_bytes
from tests.approvals.support import TARGET_A, TEST_CHAIN_KEY, fixture_draft, hand_chained
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


def test_22_self_owned_pty_yes_saying_is_denied_not_aborted(tmp_path: Path) -> None:
    """The verifier exploit: a master the same process writes 'confirm' to.

    The refusal fires at the controlling-terminal gate BEFORE the
    confirmation is ever read, and the store area is untouched.
    """

    store_dir = tmp_path / "records"
    store_dir.mkdir()
    before = snapshot_tree(store_dir)
    master, slave = pty.openpty()
    try:
        for _answer in (b"confirm\n", b"yes-please-approve-everything\n"):
            with pytest.raises(IngressRefusalError) as error:
                record_operation(
                    purpose="editorial",
                    target_bundle_hash=TARGET_A,
                    decision="approve",
                    actor_id="attacker-self-owned-pty",
                    tty_fd=slave,
                )
            assert error.value.code in (
                "no-controlling-terminal",
                "controlling-terminal-mismatch",
                "not-a-tty",
            )
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
        validate_supersession_chain((forged,), chain_key=TEST_CHAIN_KEY)
    verdict = evaluate_authorization(
        (forged,),
        purpose="editorial",
        target_hash=TARGET_A,
        target_type="edit-plan",
        operator_gate=True,
    )
    assert verdict.authorized is False


def _records_file(tmp_path: Path) -> Path:
    """A store whose honest chain was minted under its own local key."""

    store = OperationRecordStore(tmp_path / "operation-records.jsonl")
    store.append(fixture_draft())
    return store.records_path


def test_40_offline_forged_unkeyed_chain_fails_verification(tmp_path: Path) -> None:
    """The verifier's reforge exploit: rewrite lines with fresh sha256 seals."""

    records_path = _records_file(tmp_path)
    store = OperationRecordStore(records_path)
    assert len(store.verify_chain()) == 1

    forged_lines: list[bytes] = []
    previous = "0" * 64
    for seq, line in enumerate(records_path.read_bytes().splitlines(), start=1):
        record = ChainedOperationRecord.model_validate_json(line)
        mutated = record.model_copy(
            update={
                "actor_id": "attacker",
                "timestamp_seq": seq,
                "superseded_record_id": None,
                "previous_record_hash": previous,
                "record_hash": "0" * 64,
            }
        )
        unkeyed = hashlib.sha256(
            canonical_json_bytes(mutated.model_copy(update={"record_hash": "0" * 64}))
        ).hexdigest()
        sealed = mutated.model_copy(update={"record_hash": unkeyed})
        forged_lines.append(sealed.model_dump_json().encode())
        previous = unkeyed
    records_path.write_bytes(b"\n".join(forged_lines) + b"\n")

    with pytest.raises(OperationRecordError, match="chain-invalid"):
        OperationRecordStore(records_path).verify_chain()


def test_41_forged_chain_under_a_foreign_key_fails(tmp_path: Path) -> None:
    records_path = _records_file(tmp_path)
    foreign_key = b"attacker-knows-a-different-key-00000000000"
    forged = hand_chained(seq=1, chain_key=foreign_key, tamper=False)
    records_path.write_bytes(forged.model_dump_json().encode() + b"\n")
    with pytest.raises(OperationRecordError, match="chain-invalid"):
        OperationRecordStore(records_path).verify_chain()


def test_42_deleted_key_file_fails_closed(tmp_path: Path) -> None:
    records_path = _records_file(tmp_path)
    key_file = records_path.with_name(records_path.name + ".hmac-key")
    assert key_file.is_file()
    key_file.unlink()
    with pytest.raises(OperationRecordError, match="chain-key-missing"):
        OperationRecordStore(records_path).verify_chain()
    with pytest.raises(ChainKeyError, match="chain-key-missing"):
        load_chain_key(records_path, create=False)


def test_43_env_key_override_is_a_test_seam_not_a_backdoor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records sealed under env key A fail verification under key B."""

    records_path = tmp_path / "operation-records.jsonl"
    monkeypatch.setenv(CHAIN_KEY_ENV, "ab" * 32)
    store = OperationRecordStore(records_path)
    store.append(fixture_draft())
    assert len(store.verify_chain()) == 1
    monkeypatch.setenv(CHAIN_KEY_ENV, "cd" * 32)
    with pytest.raises(OperationRecordError, match="chain-invalid"):
        store.verify_chain()


def test_44_key_file_is_created_private_and_hex(tmp_path: Path) -> None:
    records_path = _records_file(tmp_path)
    key_file = records_path.with_name(records_path.name + ".hmac-key")
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600
    key = load_chain_key(records_path, create=False)
    assert len(key) == 32
