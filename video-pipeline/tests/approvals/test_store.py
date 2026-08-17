from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.approvals.store import OperationRecordError
from tests.approvals.support import TARGET_B, fixture_draft, make_store


def test_append_assigns_deterministic_ids_and_sequence(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.append(fixture_draft())
    second = store.append(fixture_draft(target=TARGET_B))
    assert first.record_id == "opr-00000001"
    assert first.timestamp_seq == 1
    assert second.record_id == "opr-00000002"
    assert second.previous_record_hash == first.record_hash


def test_supersession_links_new_record_and_latest_query(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.append(fixture_draft(decision="approve"))
    second = store.append(fixture_draft(decision="reject"))
    assert second.superseded_record_id == first.record_id
    latest = store.latest_records()
    assert latest[("editorial", first.target_hash)].record_id == second.record_id
    assert len(store.all_records()) == 2


def test_superseded_record_retained_for_audit(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.append(fixture_draft(decision="approve"))
    store.append(fixture_draft(decision="reject"))
    lines = store.records_path.read_text().splitlines()
    assert len(lines) == 2
    assert first.record_id in json.loads(lines[0])["record_id"]


def test_distinct_targets_do_not_supersede(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    second = store.append(fixture_draft(target=TARGET_B))
    assert second.superseded_record_id is None
    assert len(store.latest_records()) == 2


def test_verify_chain_ok_after_appends(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft(decision="approve"))
    store.append(fixture_draft(decision="reject"))
    store.append(fixture_draft(target=TARGET_B))
    assert len(store.verify_chain()) == 3


def test_content_tamper_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    raw = json.loads(store.records_path.read_text())
    raw["actor_id"] = "attacker"
    store.records_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    with pytest.raises(OperationRecordError) as error:
        store.verify_chain()
    assert error.value.code == "chain-invalid"


def test_malformed_line_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    store.records_path.write_text('{"purpose": "editorial"\n')
    with pytest.raises(OperationRecordError) as error:
        store.verify_chain()
    assert error.value.code == "record-line-invalid"


def test_reordered_lines_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    store.append(fixture_draft(decision="reject"))
    lines = store.records_path.read_text().splitlines()
    store.records_path.write_text("\n".join(reversed(lines)))
    with pytest.raises(OperationRecordError) as error:
        store.verify_chain()
    assert error.value.code == "chain-invalid"


def test_interior_deletion_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    store.append(fixture_draft(decision="reject"))
    store.append(fixture_draft(target=TARGET_B))
    lines = store.records_path.read_text().splitlines()
    store.records_path.write_text("\n".join((lines[0], lines[2])) + "\n")
    with pytest.raises(OperationRecordError) as error:
        store.verify_chain()
    assert error.value.code == "chain-invalid"


def test_append_on_tampered_chain_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append(fixture_draft())
    raw = json.loads(store.records_path.read_text())
    raw["decision"] = "reject"
    store.records_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    with pytest.raises(OperationRecordError) as error:
        store.append(fixture_draft(target=TARGET_B))
    assert error.value.code == "chain-invalid"
