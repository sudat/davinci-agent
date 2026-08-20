"""TTY approval ingress tests (hardened for the Todo-66 fix round).

The primary adversarial case: a SELF-OWNED pty (``pty.openpty`` with the
same process writing ``confirm`` to the master) satisfies ``isatty`` but
is NOT the controlling terminal — it must be refused typed, with zero
store mutation. The happy paths run in a child process that legitimately
acquires the pty as its controlling terminal (session leader open,
exactly how a login attaches a terminal), proving real terminals still
work end to end.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from services.approvals.ingress import (
    IngressRefusalError,
    controlling_terminal_name,
    record_fixture_operation,
    record_operation,
)
from services.approvals.store import OperationRecordStore
from tests.approvals.support import (
    ACTOR,
    FINAL_BINDING,
    TARGET_A,
    wait_with_master_drain,
)

REFUSAL_BEFORE_PROMPT = (
    "no-controlling-terminal",
    "controlling-terminal-mismatch",
    "not-a-tty",
)


def ingress_over_self_owned_pty(
    *,
    confirm: bytes,
    runner_class: str = "operator",
    fixture: bool = False,
    purpose: str = "editorial",
):
    """The verifier's exploit: same process owns master AND slave."""

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
            final_binding=FINAL_BINDING if purpose == "final" else None,
        )
    finally:
        os.close(master)
        os.close(slave)


def ingress_over_controlling_tty(
    *,
    confirm: bytes,
    fixture: bool = False,
    purpose: str = "editorial",
    timeout: int = 30,
) -> dict[str, object]:
    """Run the ingress in a child whose controlling terminal IS the pty."""

    master, slave = pty.openpty()
    tty_path = os.ttyname(slave)
    result_path = Path(tempfile.mkdtemp(prefix="ctty-")) / "result.json"
    payload = json.dumps(
        {
            "purpose": purpose,
            "target_bundle_hash": TARGET_A,
            "decision": "approve",
            "actor_id": ACTOR,
            "runner_class": "operator",
            "fixture": fixture,
            "final_binding": (
                FINAL_BINDING.model_dump(mode="json") if purpose == "final" else None
            ),
        }
    )
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.approvals.ctty_child",
                tty_path,
                "--ingress",
                str(result_path),
                payload,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=Path.cwd(),
        )
        os.write(master, confirm)
        wait_with_master_drain(master, process, timeout)
        child_stderr = process.stderr.read() if process.stderr else ""
        if process.stderr is not None:
            process.stderr.close()
    finally:
        os.close(master)
        os.close(slave)
    if not result_path.is_file():
        raise AssertionError(f"ctty child produced no result: {child_stderr[-800:]}")
    return json.loads(result_path.read_text(encoding="utf-8"))


def test_10_self_owned_pty_cannot_mint_an_operator_record(tmp_path: Path) -> None:
    store = OperationRecordStore(tmp_path / "operation-records.jsonl")
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(IngressRefusalError) as error:
        ingress_over_self_owned_pty(confirm=b"confirm\n")

    assert error.value.code in REFUSAL_BEFORE_PROMPT
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # zero store mutation
    assert store.all_records() == ()


def test_11_self_owned_pty_denial_ignores_a_yes_saying_master() -> None:
    for payload in (b"confirm\n", b"confirm", b"yes\n", b""):
        with pytest.raises(IngressRefusalError) as error:
            ingress_over_self_owned_pty(confirm=payload)
        assert error.value.code in REFUSAL_BEFORE_PROMPT


def test_12_self_owned_pty_fixture_mode_is_denied_too(tmp_path: Path) -> None:
    """Even fixture-marked TTY records require a real controlling terminal."""

    with pytest.raises(IngressRefusalError) as error:
        ingress_over_self_owned_pty(confirm=b"confirm\n", fixture=True)
    assert error.value.code in REFUSAL_BEFORE_PROMPT
    assert list(tmp_path.iterdir()) == []


def test_13_runner_class_validated_at_the_boundary() -> None:
    for forged in ("Operator", "automation ", "OPERATOR", "", "human", 1):
        with pytest.raises(IngressRefusalError) as error:
            record_operation(
                purpose="editorial",
                target_bundle_hash=TARGET_A,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=None,
                runner_class=forged,  # type: ignore[arg-type]
            )
        assert error.value.code == "invalid-runner-class"


def test_14_automation_refusal_precedes_the_tty_gate() -> None:
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


def test_20_controlling_terminal_child_records_uid_and_tty() -> None:
    result = ingress_over_controlling_tty(confirm=b"confirm\n")
    assert result["ok"] is True
    draft = result["draft"]
    assert isinstance(draft, dict)
    assert draft["fixture_only"] is False
    assert draft["uid"] == os.getuid()
    assert str(draft["tty"]).startswith("/dev/")
    assert draft["runner_class"] == "operator"


def test_21_controlling_terminal_wrong_confirmation_aborts() -> None:
    result = ingress_over_controlling_tty(confirm=b"nope\n")
    assert result["ok"] is False
    assert result["code"] == "operator-aborted"


def test_22_controlling_terminal_fixture_mode_stays_fixture_marked() -> None:
    result = ingress_over_controlling_tty(confirm=b"confirm\n", fixture=True)
    assert result["ok"] is True
    draft = result["draft"]
    assert isinstance(draft, dict)
    assert draft["fixture_only"] is True


def test_23_controlling_terminal_final_purpose_carries_binding() -> None:
    result = ingress_over_controlling_tty(confirm=b"confirm\n", purpose="final")
    assert result["ok"] is True
    draft = result["draft"]
    assert isinstance(draft, dict)
    assert draft["purpose"] == "final"
    assert isinstance(draft["final_binding"], dict)


def test_30_this_test_process_has_no_controlling_terminal_by_default() -> None:
    """The QA harness runs detached; the exploit dies at the first gate."""

    name = controlling_terminal_name()
    if name is not None:
        pytest.skip("outer harness unexpectedly has a controlling terminal")
    with pytest.raises(IngressRefusalError) as error:
        ingress_over_self_owned_pty(confirm=b"confirm\n")
    assert error.value.code == "no-controlling-terminal"


def test_31_non_tty_descriptors_refused(tmp_path: Path) -> None:
    devnull = os.open(os.devnull, os.O_RDONLY)
    read_end, write_end = os.pipe()
    try:
        for fd in (devnull, read_end):
            with pytest.raises(IngressRefusalError) as error:
                record_operation(
                    purpose="editorial",
                    target_bundle_hash=TARGET_A,
                    decision="approve",
                    actor_id=ACTOR,
                    tty_fd=fd,
                )
            assert error.value.code == "not-a-tty"
    finally:
        os.close(devnull)
        os.close(read_end)
        os.close(write_end)
    assert list(tmp_path.iterdir()) == []


def test_32_missing_tty_fd_refused() -> None:
    with pytest.raises(IngressRefusalError) as error:
        record_operation(
            purpose="editorial",
            target_bundle_hash=TARGET_A,
            decision="approve",
            actor_id=ACTOR,
            tty_fd=None,
        )
    assert error.value.code == "not-a-tty"


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
            final_binding=FINAL_BINDING if purpose == "final" else None,
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
