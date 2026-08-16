from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.evidence.append_event import append_freeze_event
from services.fixtures.models import FreezeIntent, Phase0AFixtureManifest
from services.fixtures.prepare import PrepareError, PrepareRequest, prepare_freeze
from services.fixtures.publish import PublishError, publish_freeze
from services.foundation_io import canonical_model_bytes
from services.toolchain.execution_ledger import ExecutionLedgerError
from services.toolchain.models import SmokeRecord, load_lock, write_lock

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
REAL_INTENT = ATTEMPT / "freeze/6/intent.json"
REAL_LEDGER = ATTEMPT / "execution-ledger.jsonl"


def test_manifest_is_canonical_and_valid() -> None:
    raw = MANIFEST.read_bytes()
    manifest = Phase0AFixtureManifest.model_validate_json(raw)

    assert manifest.fixture_id == "p0a-cfr30-fixed"
    assert raw == manifest.canonical_bytes()


def test_recipe_has_exact_cfr_audio_and_linked_cut_values() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())

    assert manifest.recipe.source.duration_frames == 600
    assert manifest.recipe.source.frame_rate.num == 30
    assert manifest.recipe.source.frame_rate.den == 1
    assert manifest.recipe.audio.pulse_sample_positions == (0, 240000, 480000, 720000)
    assert tuple(cut.av_link_id for cut in manifest.recipe.cuts) == (
        "av-cut-001",
        "av-cut-002",
    )


def test_recipe_is_deterministic_across_key_order() -> None:
    payload = json.loads(MANIFEST.read_bytes())
    reordered = dict(reversed(tuple(payload.items())))

    first = Phase0AFixtureManifest.model_validate(payload)
    second = Phase0AFixtureManifest.model_validate(reordered)

    assert first.canonical_bytes() == second.canonical_bytes()


def test_later_phase_fixture_id_is_rejected() -> None:
    payload = json.loads(MANIFEST.read_bytes())
    payload["fixture_id"] = "p0b-cfr24"

    with pytest.raises(ValueError, match="p0a-cfr30-fixed"):
        Phase0AFixtureManifest.model_validate(payload)


def _genesis_ledger(path: Path, *, incomplete_tail: bool = False) -> None:
    genesis = {
        "event_type": "execution-start",
        "previous_event_hash": "0" * 64,
        "sequence": 1,
    }
    raw = json.dumps(genesis, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if incomplete_tail:
        raw += b'{"event_type":'
    path.write_bytes(raw)


def _copy_intent(tmp_path: Path) -> tuple[Path, FreezeIntent]:
    original = FreezeIntent.model_validate_json(REAL_INTENT.read_bytes())
    intent = original.model_copy(
        update={
            "policy_path": str(tmp_path / "phase-0a-v1.json"),
            "receipt_path": str(tmp_path / "task-6-freeze-receipt.json"),
        }
    )
    path = tmp_path / "intent.json"
    path.write_bytes(canonical_model_bytes(intent))
    return path, intent


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


def test_publish_recovers_temp_only_state(tmp_path: Path) -> None:
    intent_path, intent = _copy_intent(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)
    append_freeze_event(ledger, intent, "freeze-intent")
    policy = Path(intent.policy_path)
    receipt = Path(intent.receipt_path)
    policy.parent.mkdir(parents=True, exist_ok=True)
    (policy.parent / f".{policy.name}.freeze-tmp").write_bytes(
        Path(intent.staged_policy_path).read_bytes()
    )
    (receipt.parent / f".{receipt.name}.freeze-tmp").write_bytes(
        Path(intent.staged_receipt_path).read_bytes()
    )

    publish_freeze(intent_path, ledger, recover=True)

    assert policy.is_file()
    assert receipt.is_file()


def test_same_version_refreeze_is_rejected() -> None:
    request = PrepareRequest(
        toolchain_lock=Path("config/toolchains/phase-0a-v1.json"),
        fixture_ids=("p0a-cfr30-fixed",),
        pre_source_snapshot=ATTEMPT / "task-6-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=Path("config/gates/phase-0a-v1.json"),
        freeze_receipt=ATTEMPT / "task-6-freeze-receipt.json",
        staging=ATTEMPT / "freeze/6/prepared",
        intent=REAL_INTENT,
    )

    with pytest.raises(PrepareError, match="re-freeze"):
        prepare_freeze(request)


def test_incomplete_toolchain_is_rejected(tmp_path: Path) -> None:
    lock = load_lock(Path("config/toolchains/phase-0a-v1.json"))
    smoke = lock.smoke.model_copy(
        update={
            "ffmpeg_probe": SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending fixture",
            )
        }
    )
    lock_path = tmp_path / "lock.json"
    write_lock(lock_path, lock.model_copy(update={"smoke": smoke}))
    request = PrepareRequest(
        toolchain_lock=lock_path,
        fixture_ids=("p0a-cfr30-fixed",),
        pre_source_snapshot=ATTEMPT / "task-6-pre-source.json",
        execution_contract=ATTEMPT / "execution-contract.json",
        policy_out=tmp_path / "policy.json",
        freeze_receipt=tmp_path / "receipt.json",
        staging=tmp_path / "prepared",
        intent=tmp_path / "intent.json",
    )

    with pytest.raises(PrepareError, match="incomplete"):
        prepare_freeze(request)


def test_noncanonical_prepared_policy_is_rejected(tmp_path: Path) -> None:
    intent_path, copied = _copy_intent(tmp_path)
    staged_policy = tmp_path / "prepared-policy.json"
    payload = json.loads(Path(copied.staged_policy_path).read_bytes())
    staged_policy.write_text(json.dumps(payload, indent=2))
    intent = copied.model_copy(update={"staged_policy_path": str(staged_policy)})
    intent_path.write_bytes(canonical_model_bytes(intent))
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)
    append_freeze_event(ledger, intent, "freeze-intent")

    with pytest.raises(PublishError, match="noncanonical"):
        publish_freeze(intent_path, ledger, recover=True)


def test_corrupt_intent_copy_is_rejected(tmp_path: Path) -> None:
    intent_path = tmp_path / "intent.json"
    intent_path.write_bytes(REAL_INTENT.read_bytes() + b" ")
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)

    with pytest.raises(PublishError, match="noncanonical freeze intent"):
        publish_freeze(intent_path, ledger, recover=True)


def test_incomplete_final_ledger_fragment_is_repaired(tmp_path: Path) -> None:
    _intent_path, intent = _copy_intent(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger, incomplete_tail=True)

    event = append_freeze_event(ledger, intent, "freeze-intent")

    assert event.sequence == 2
    assert len(ledger.read_bytes().splitlines()) == 2


def test_complete_invalid_ledger_tail_blocks_recovery(tmp_path: Path) -> None:
    _intent_path, intent = _copy_intent(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    _genesis_ledger(ledger)
    forged = {
        "event_type": "forged",
        "previous_event_hash": "0" * 64,
        "sequence": 2,
    }
    with ledger.open("ab") as stream:
        stream.write(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    with pytest.raises(ExecutionLedgerError, match="chain drift"):
        append_freeze_event(ledger, intent, "freeze-intent")
