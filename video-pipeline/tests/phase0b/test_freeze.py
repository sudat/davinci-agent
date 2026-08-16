from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.evidence.append_event import append_freeze_event
from services.fixtures.models import FreezeIntent
from services.fixtures.prepare import PrepareError
from services.fixtures.prepare_phase0b import (
    Phase0BPrepareRequest,
    prepare_phase0b,
)
from services.fixtures.publish import PublishError, publish_freeze
from services.foundation_io import canonical_model_bytes
from services.gates import PHASE_0B_VARIANTS
from services.toolchain.models import Phase0BToolchainLock, load_lock

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
REAL_INTENT = ATTEMPT / "freeze/24/intent.json"
PARENT_RESULT = ATTEMPT / "phase-0a/gate-result.json"


def _request(tmp_path: Path, **overrides: Path) -> Phase0BPrepareRequest:
    defaults: dict[str, Path] = {
        "toolchain_lock": Path("config/toolchains/phase-0b-v1.json"),
        "parent_result": PARENT_RESULT,
        "pre_source_snapshot": ATTEMPT / "task-24-pre-source.json",
        "execution_contract": ATTEMPT / "execution-contract.json",
        "policy_out": tmp_path / "policy.json",
        "freeze_receipt": tmp_path / "receipt.json",
        "staging": tmp_path / "prepared",
        "intent": tmp_path / "intent.json",
    }
    defaults.update(overrides)
    return Phase0BPrepareRequest(
        toolchain_lock=defaults["toolchain_lock"],
        fixture_ids=PHASE_0B_VARIANTS,
        parent_result=defaults["parent_result"],
        pre_source_snapshot=defaults["pre_source_snapshot"],
        execution_contract=defaults["execution_contract"],
        policy_out=defaults["policy_out"],
        freeze_receipt=defaults["freeze_receipt"],
        staging=defaults["staging"],
        intent=defaults["intent"],
    )


def test_same_version_refreeze_is_rejected() -> None:
    request = Phase0BPrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0b-v1.json"),
        fixture_ids=PHASE_0B_VARIANTS,
        parent_result=PARENT_RESULT,
        pre_source_snapshot=ATTEMPT / "task-24-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=Path("config/gates/phase-0b-v1.json"),
        freeze_receipt=ATTEMPT / "task-24-freeze-receipt.json",
        staging=ATTEMPT / "freeze/24/prepared",
        intent=REAL_INTENT,
    )
    with pytest.raises(PrepareError, match="re-freeze"):
        prepare_phase0b(request)


def test_stale_failing_parent_result_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(PARENT_RESULT.read_bytes())
    payload["passed"] = False
    for result in payload["criteria_results"]:
        result["passed"] = False
    stale = tmp_path / "stale-parent.json"
    stale.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(PrepareError, match="did not pass"):
        prepare_phase0b(_request(tmp_path, parent_result=stale))


def test_tampered_parent_policy_binding_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(PARENT_RESULT.read_bytes())
    payload["policy_sha256"] = "f" * 64
    tampered = tmp_path / "tampered-parent.json"
    tampered.write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(PrepareError, match="different phase-0a policy"):
        prepare_phase0b(_request(tmp_path, parent_result=tampered))


def test_missing_variant_tool_hash_is_rejected(tmp_path: Path) -> None:
    lock = load_lock(Path("config/toolchains/phase-0b-v1.json"))
    drifted = lock.model_copy(
        update={
            "ffmpeg": lock.ffmpeg.model_copy(
                update={
                    "ffmpeg": lock.ffmpeg.ffmpeg.model_copy(
                        update={"sha256": "0" * 64},
                    )
                }
            )
        }
    )
    drifted_path = tmp_path / "drifted-lock.json"
    drifted_path.write_bytes(canonical_model_bytes(drifted))
    with pytest.raises((PrepareError,), match=r"drift|hash"):
        prepare_phase0b(_request(tmp_path, toolchain_lock=drifted_path))


def test_incomplete_normalize_smoke_is_rejected(tmp_path: Path) -> None:
    lock = load_lock(Path("config/toolchains/phase-0b-v1.json"))
    assert isinstance(lock, Phase0BToolchainLock)
    pending = lock.smoke.ffmpeg_normalize.model_copy(update={"status": "pending"})
    incomplete = lock.model_copy(
        update={"smoke": lock.smoke.model_copy(update={"ffmpeg_normalize": pending})}
    )
    lock_path = tmp_path / "pending-lock.json"
    lock_path.write_bytes(canonical_model_bytes(incomplete))
    with pytest.raises(PrepareError, match="incomplete"):
        prepare_phase0b(_request(tmp_path, toolchain_lock=lock_path))


def _copy_intent(tmp_path: Path) -> tuple[Path, FreezeIntent]:
    original = FreezeIntent.model_validate_json(REAL_INTENT.read_bytes())
    intent = original.model_copy(
        update={
            "policy_path": str(tmp_path / "phase-0b-v1.json"),
            "receipt_path": str(tmp_path / "task-24-freeze-receipt.json"),
        }
    )
    path = tmp_path / "intent.json"
    path.write_bytes(canonical_model_bytes(intent))
    return path, intent


def _genesis_ledger(path: Path) -> None:
    genesis = {
        "event_type": "execution-start",
        "previous_event_hash": "0" * 64,
        "sequence": 1,
    }
    raw = json.dumps(genesis, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(raw)


def test_publish_recovers_policy_only_and_both_states(tmp_path: Path) -> None:
    intent_path, intent = _copy_intent(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)
    append_freeze_event(ledger, intent, "freeze-intent")
    Path(intent.policy_path).write_bytes(Path(intent.staged_policy_path).read_bytes())

    publish_freeze(intent_path, ledger, recover=True)
    publish_freeze(intent_path, ledger, recover=True)

    assert Path(intent.receipt_path).is_file()
    assert len(ledger.read_bytes().splitlines()) == 3


def test_corrupt_intent_copy_is_rejected(tmp_path: Path) -> None:
    intent_path = tmp_path / "intent.json"
    intent_path.write_bytes(REAL_INTENT.read_bytes() + b" ")
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)

    with pytest.raises(PublishError, match="noncanonical freeze intent"):
        publish_freeze(intent_path, ledger, recover=True)


def test_corrupt_lock_copy_is_rejected_by_prepare(tmp_path: Path) -> None:
    lock_path = tmp_path / "corrupt-lock.json"
    lock_path.write_bytes(b'{"schema_version":')
    with pytest.raises(PrepareError, match="lock"):
        prepare_phase0b(_request(tmp_path, toolchain_lock=lock_path))


def test_intent_append_is_idempotent_for_identical_event(tmp_path: Path) -> None:
    _intent_path, intent = _copy_intent(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)
    first = append_freeze_event(ledger, intent, "freeze-intent")
    second = append_freeze_event(ledger, intent, "freeze-intent")
    assert first.sequence == second.sequence
    assert len(ledger.read_bytes().splitlines()) == 2
