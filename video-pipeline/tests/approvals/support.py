from __future__ import annotations

import hashlib
from pathlib import Path

from services.approvals.models import ChainedOperationRecord, OperationDraft
from services.approvals.store import GENESIS_RECORD_HASH, OperationRecordStore

TARGET_A = hashlib.sha256(b"approvals-test-target-a").hexdigest()
TARGET_B = hashlib.sha256(b"approvals-test-target-b").hexdigest()
ACTOR = "test-operator"


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
    sealed_hash = "f" * 64 if tamper else unsealed.recomputed_record_hash()
    return unsealed.model_copy(update={"record_hash": sealed_hash})
