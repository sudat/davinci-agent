from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import time
from pathlib import Path

from services.approvals.global_review_models import FinalReviewBinding
from services.approvals.models import ChainedOperationRecord, OperationDraft
from services.approvals.store import GENESIS_RECORD_HASH, OperationRecordStore

TEST_CHAIN_KEY = b"todo-66-fix-test-chain-key-0000000000000"


def wait_with_master_drain(
    master: int,
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    timeout: float,
) -> None:
    """Wait for a ctty-child while draining the pty master.

    A macOS session leader whose controlling terminal still has unread
    output blocks in exit until the master reader drains it, so poll and
    drain non-blocking until the child is gone (bounded by ``timeout``).
    """

    os.set_blocking(master, False)
    deadline = time.monotonic() + timeout
    try:
        while True:
            with contextlib.suppress(BlockingIOError):
                os.read(master, 65536)
            if process.poll() is not None:
                return
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            time.sleep(0.05)
    finally:
        os.set_blocking(master, True)

TARGET_A = hashlib.sha256(b"approvals-test-target-a").hexdigest()
TARGET_B = hashlib.sha256(b"approvals-test-target-b").hexdigest()
ACTOR = "test-operator"

FINAL_BINDING = FinalReviewBinding(
    work_id="work-approvals-test",
    full_sha="1f" * 20,
    candidate_id="cand-approvals-test",
    report_set_sha256=hashlib.sha256(b"approvals-test-report-set").hexdigest(),
)


def make_store(tmp_path: Path) -> OperationRecordStore:
    return OperationRecordStore(tmp_path / "operation-records.jsonl")


def fixture_draft(
    *,
    purpose: str = "editorial",
    target: str = TARGET_A,
    decision: str = "approve",
    actor: str = ACTOR,
) -> OperationDraft:
    target_type = {
        "editorial": "edit-plan",
        "presentation": "presentation-bundle",
        "privacy": "privacy-report",
        "rights": "rights-report",
        "final": "final-render",
        "publication": "publication-bundle",
        "manual_freeze": "frozen-timeline",
    }[purpose]
    return OperationDraft.model_validate(
        {
            "purpose": purpose,
            "target_type": target_type,
            "target_hash": target,
            "decision": decision,
            "actor_id": actor,
            "uid": None,
            "tty": None,
            "wall_time_unix": None,
            "fixture_only": True,
            "runner_class": "automation",
            "final_binding": FINAL_BINDING if purpose == "final" else None,
        }
    )


def hand_chained(  # noqa: PLR0913 (explicit record-shape builder for chain tests)
    *,
    seq: int,
    previous_hash: str = GENESIS_RECORD_HASH,
    purpose: str = "editorial",
    target: str = TARGET_A,
    decision: str = "approve",
    superseded: str | None = None,
    tamper: bool = False,
    chain_key: bytes = TEST_CHAIN_KEY,
) -> ChainedOperationRecord:
    draft = fixture_draft(purpose=purpose, target=target, decision=decision)
    unsealed = ChainedOperationRecord.model_validate(
        {
            **draft.model_dump(),
            "record_id": f"opr-{seq:08d}",
            "timestamp_seq": seq,
            "superseded_record_id": superseded,
            "previous_record_hash": previous_hash,
            "record_hash": GENESIS_RECORD_HASH,
        }
    )
    sealed_hash = "f" * 64 if tamper else unsealed.recomputed_record_hash(chain_key=chain_key)
    return unsealed.model_copy(update={"record_hash": sealed_hash})
