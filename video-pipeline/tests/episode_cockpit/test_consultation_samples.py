"""Wave-1 consultation sample storage: idempotency, conflict, recovery.

Identity-keyed request semantics (v4 contract 訂正3 P1-B + F1/F2/F7,
P1-3/P1-5): a fully matching resend returns the stored result with zero
new journal writes; the same operation_id with a different identity is
a typed 409; a published sample missing its success journal line
recovers it exactly once after FULL verification (video hash +
identity + full-IR re-derivation of sample sha/lineage + budget/run/
attempt cross-checks); manifest tampering fails closed with zero
writes. The fresh reserve is journal-only (no final dir); a resend
after failure re-attempts with a linked attempt id; concurrent
same-identity requests yield one reserve.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from services.compile.sample_projection import project_sample_ir
from services.contracts.primitives import RecordFrameSpan
from services.episode_cockpit.consultation_selection_budget import (
    attempt_for,
    load_selection_budget_entries,
    reserve_preview,
    settle_preview,
)
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_complete import write_published_manifest
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SampleManifestV1,
    SampleRequestIdentityV1,
    derive_sample_id,
)
from services.episode_cockpit.sample_journal import (
    request_sample,
    sample_dir,
    settle_sample_failed,
)
from services.foundation_io import canonical_model_bytes
from tests.compile.test_sample_projection import _full_ir

if TYPE_CHECKING:
    from services.outputs.geometry import OutputId


def _write_full_ir(tmp_path: Path, base_version: str = "v1") -> str:
    """Store the fixture full IR server-side; return its file sha."""
    raw = canonical_model_bytes(_full_ir())
    path = tmp_path / "review" / "store" / f"ir-{base_version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _identity(
    tmp_path: Path,
    operation_id: str = "op-1",
    *,
    windows: tuple[tuple[int, int], ...] = ((0, 30),),
    output_id: OutputId = "landscape",
    full_ir: str | None = None,
) -> SampleRequestIdentityV1:
    return SampleRequestIdentityV1(
        episode_id="ep-s1",
        consultation_id="c1",
        judgment_id="j1",
        base_version="v1",
        base_plan_sha256="a" * 64,
        policy_sha256="b" * 64,
        output_id=output_id,
        windows=tuple(
            RecordFrameSpan(start_frame=start, end_frame=end)
            for start, end in windows
        ),
        operation_id=operation_id,
        full_ir_sha256=full_ir or _write_full_ir(tmp_path),
    )


def _publish_final(
    tmp_path: Path, identity: SampleRequestIdentityV1, *, video: bytes = b"sample-bytes",
) -> SampleManifestV1:
    """Craft a fully cross-checked final dir (reserve + budget + manifest + video)."""
    request = request_sample(tmp_path, identity)
    assert request["state"] == "reserved"
    sample_attempt = request["sample_attempt_id"]
    budget = attempt_for(1, "c1", "j1")
    reserve_preview(tmp_path, budget, 1.0)
    settle_preview(
        tmp_path, budget, preview_seconds=1.0, wall_elapsed=0.05, result="succeeded",
    )
    sample = project_sample_ir(_full_ir(), list(identity.windows))
    target = sample_dir(tmp_path, derive_sample_id(identity))
    target.mkdir(parents=True, exist_ok=True)
    (target / SAMPLE_PREVIEW_NAME).write_bytes(video)
    return write_published_manifest(
        target,
        identity,
        sample_ir=sample,
        total_seconds=1.0,
        content_sha256=hashlib.sha256(video).hexdigest(),
        sample_attempt_id=sample_attempt,
        budget_entry_id=budget.attempt_id,
        budget_reservation_sequence=budget.reservation_sequence,
        run_id="run-final-0001",
        wall_seconds_used=0.05,
    )


def _tamper_manifest(tmp_path: Path, sample_id: str, **fields: Any) -> None:
    path = sample_dir(tmp_path, sample_id) / SAMPLE_MANIFEST_NAME
    raw = json.loads(path.read_bytes())
    raw.update(fields)
    path.write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())


def _journal_lines(tmp_path: Path) -> list[bytes]:
    journal = tmp_path / "consultation" / "samples.jsonl"
    if not journal.exists():
        return []
    return journal.read_bytes().splitlines()


def _event_names(tmp_path: Path) -> list[str]:
    return [json.loads(line)["event"] for line in _journal_lines(tmp_path)]


def test_resend_returns_stored_without_new_writes(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    assert request_sample(tmp_path, identity)["state"] == "recovering"
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    result = request_sample(tmp_path, identity)
    assert result["state"] == "stored"
    assert result["manifest"].status == "published"
    assert journal.read_bytes() == before  # zero new writes


def test_same_operation_id_different_identity_is_409(tmp_path: Path) -> None:
    request_sample(tmp_path, _identity(tmp_path, "op-1", windows=((0, 30),)))
    with pytest.raises(CockpitConflictError) as exc_info:
        request_sample(tmp_path, _identity(tmp_path, "op-1", windows=((5, 35),)))
    assert exc_info.value.code == "sample-request-conflict"


def test_published_without_success_journal_recovers_once(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    result = request_sample(tmp_path, identity)
    assert result["state"] == "recovering"
    assert result["manifest"].status == "published"
    again = request_sample(tmp_path, identity)
    assert again["state"] == "stored"
    success_lines = [
        line for line in _journal_lines(tmp_path) if b"sample_success" in line
    ]
    assert len(success_lines) == 1  # exactly once, never duplicated


def test_fresh_request_is_journal_only_no_final_dir(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-2")
    result = request_sample(tmp_path, identity)
    assert result["state"] == "reserved"
    assert result["sample_attempt_id"] == "sample-attempt-1"
    assert result["origin_sample_attempt_id"] is None
    manifest = result["manifest"]
    assert manifest.status == "reserved"
    assert manifest.identity == identity
    assert manifest.full_ir_sha256 == identity.full_ir_sha256
    assert not sample_dir(tmp_path, derive_sample_id(identity)).exists()
    assert not (tmp_path / "consultation" / "samples").exists()
    events = _event_names(tmp_path)
    assert events == ["sample_reserved"]
    record = json.loads(_journal_lines(tmp_path)[0])
    assert record["operation_id"] == "op-2"
    assert record["sample_attempt_id"] == "sample-attempt-1"
    embedded = SampleManifestV1.model_validate(json.loads(record["detail"])["manifest"])
    assert embedded.sample_id == derive_sample_id(identity)
    assert embedded.status == "reserved"


def test_open_reserve_resend_joins_without_new_writes(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-join")
    first = request_sample(tmp_path, identity)
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    second = request_sample(tmp_path, identity)
    assert second["state"] == "reserved"
    assert second["sample_attempt_id"] == first["sample_attempt_id"]
    assert second.get("joined") is True
    assert journal.read_bytes() == before  # zero new writes on join


def test_failure_then_resend_reattempts_with_linked_attempt(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-retry")
    first = request_sample(tmp_path, identity)
    settle_sample_failed(tmp_path, identity, "render-failed: probe mismatch")
    assert b"render-failed" in (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    second = request_sample(tmp_path, identity)
    assert second["state"] == "reserved"
    assert second["sample_attempt_id"] != first["sample_attempt_id"]
    assert second["origin_sample_attempt_id"] == first["sample_attempt_id"]
    assert _event_names(tmp_path) == [
        "sample_reserved",
        "sample_failed",
        "sample_reserved",
    ]


def test_concurrent_same_identity_yields_single_reserve(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-race")
    barrier = threading.Barrier(3)
    outcomes: list[dict[str, object]] = []

    def _request() -> None:
        barrier.wait(timeout=10)
        outcomes.append(request_sample(tmp_path, identity))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_request)
        second = pool.submit(_request)
        barrier.wait(timeout=10)
        first.result()
        second.result()
    assert [outcome["state"] for outcome in outcomes] == ["reserved", "reserved"]
    reserves = [
        line for line in _journal_lines(tmp_path) if b"sample_reserved" in line
    ]
    assert len(reserves) == 1  # exactly one reserve under concurrency


def test_torn_journal_line_is_typed_parse_error(tmp_path: Path) -> None:
    journal = tmp_path / "consultation" / "samples.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b'{"event": "sample_reserved", "torn": \n')
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, _identity(tmp_path))
    assert exc_info.value.code == "sample-journal-corrupt"
    settle_sample_failed(tmp_path, _identity(tmp_path), "x")
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, _identity(tmp_path))
    assert exc_info.value.code == "sample-journal-corrupt"


def test_reorder_only_windows_share_sample_id_and_join(tmp_path: Path) -> None:
    first = request_sample(tmp_path, _identity(tmp_path, "op-order", windows=((0, 30), (60, 90))))
    reordered = _identity(tmp_path, "op-order", windows=((60, 90), (0, 30)))
    assert derive_sample_id(reordered) == derive_sample_id(first["manifest"].identity)
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    second = request_sample(tmp_path, reordered)
    assert second["state"] == "reserved"
    assert second["manifest"].sample_id == first["manifest"].sample_id
    assert journal.read_bytes() == before  # no double charge, no new reserve


def test_recovery_missing_video_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    (sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_PREVIEW_NAME).unlink()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert _event_names(tmp_path) == ["sample_reserved"]  # NO success completion


def test_recovery_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    (sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_PREVIEW_NAME).write_bytes(
        b"tampered-bytes"
    )
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert _event_names(tmp_path) == ["sample_reserved"]


def test_recovery_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    manifest_path = (
        sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_MANIFEST_NAME
    )
    manifest_path.write_bytes(b'{"sample_id": "torn", ')
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert _event_names(tmp_path) == ["sample_reserved"]


def test_recovery_identity_tamper_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    manifest = _publish_final(tmp_path, identity)
    manifest_path = (
        sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_MANIFEST_NAME
    )
    tampered = manifest.model_dump(mode="json")
    tampered["identity"]["operation_id"] = "op-evil"
    manifest_path.write_bytes(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert _event_names(tmp_path) == ["sample_reserved"]


def test_recovery_full_ir_tamper_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    manifest = _publish_final(tmp_path, identity)
    manifest_path = (
        sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_MANIFEST_NAME
    )
    tampered = manifest.model_dump(mode="json")
    tampered["full_ir_sha256"] = "0" * 64
    manifest_path.write_bytes(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert _event_names(tmp_path) == ["sample_reserved"]


def test_recovery_sample_sha_tamper_fails_closed(tmp_path: Path) -> None:
    """P1-5: a re-derived sample sha that disagrees stops with zero writes."""
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    _tamper_manifest(tmp_path, derive_sample_id(identity), sample_ir_sha256="0" * 64)
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_recovery_lineage_tamper_fails_closed(tmp_path: Path) -> None:
    """P1-5: caller-claimed lineage can never survive re-derivation."""
    identity = _identity(tmp_path)
    manifest = _publish_final(tmp_path, identity)
    tampered = manifest.model_dump(mode="json")
    tampered["lineage"] = [
        {"source_item_id": "evil", "sample_item_id": "evil.s0"},
    ]
    manifest_path = (
        sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_MANIFEST_NAME
    )
    manifest_path.write_bytes(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    )
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_recovery_budget_entry_tamper_fails_closed(tmp_path: Path) -> None:
    """P1-5: an unknown budget entry id stops with zero writes."""
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    _tamper_manifest(
        tmp_path, derive_sample_id(identity), budget_entry_id="selection-999")
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_recovery_run_id_tamper_fails_closed(tmp_path: Path) -> None:
    """P1-5: a blanked run id stops with zero writes."""
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    _tamper_manifest(tmp_path, derive_sample_id(identity), run_id="")
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_recovery_attempt_tamper_fails_closed(tmp_path: Path) -> None:
    """P1-5: an attempt id with no open reserve stops with zero writes."""
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    _tamper_manifest(
        tmp_path, derive_sample_id(identity), sample_attempt_id="sample-attempt-999")
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_swapped_full_ir_file_fails_closed(tmp_path: Path) -> None:
    """P1-5: the stored full-IR file's sha must match the claim — a swapped file stops."""
    identity = _identity(tmp_path)
    _publish_final(tmp_path, identity)
    ir_path = tmp_path / "review" / "store" / "ir-v1.json"
    ir_path.write_bytes(b'{"forged": true}')
    journal = tmp_path / "consultation" / "samples.jsonl"
    before = journal.read_bytes()
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert journal.read_bytes() == before


def test_failure_settles_journal_only(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-3")
    request_sample(tmp_path, identity)
    settle_sample_failed(tmp_path, identity, "render-failed: probe mismatch")
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert b"sample_failed" in journal
    assert b"render-failed" in journal
    assert not sample_dir(tmp_path, derive_sample_id(identity)).exists()


def test_journal_line_validates_strictly(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SampleManifestV1.model_validate(
            {
                "sample_id": "sample-x",
                "identity": _identity(tmp_path).model_dump(mode="json"),
                "total_seconds": 1.0,
                "status": "bogus",
                "created_at": "now",
            }
        )


def test_selection_ledger_tie_breaks_recovery(tmp_path: Path) -> None:
    """The manifest budget entry must name a real ledger line for this judgment."""
    identity = _identity(tmp_path)
    manifest = _publish_final(tmp_path, identity)
    entries = load_selection_budget_entries(tmp_path)
    assert [e.attempt_id for e in entries if e.phase == "preview_settled"] == [
        manifest.budget_entry_id
    ]


def test_manifest_id_fields_reject_non_identifier(tmp_path: Path) -> None:
    """P2-1: budget_entry_id/run_id/sample_attempt_id reuse Identifier —
    values outside the charset fail validation instead of persisting."""
    identity = _identity(tmp_path)
    manifest = _publish_final(tmp_path, identity)
    base = manifest.model_dump(mode="json")
    for field in ("budget_entry_id", "run_id", "sample_attempt_id"):
        with pytest.raises(ValidationError):
            SampleManifestV1.model_validate({**base, field: "has space"})


def test_prefix_collision_join_refused(tmp_path: Path) -> None:
    """P2-1: the 16-hex sample_id truncation is collision-reject-safe —
    an open reserve whose full digest differs from the request never
    joins; it fails closed as sample-request-conflict."""
    identity = _identity(tmp_path, "op-collide")
    request_sample(tmp_path, identity)
    journal = tmp_path / "consultation" / "samples.jsonl"
    lines = [
        json.loads(line) for line in journal.read_bytes().splitlines()
    ]
    assert len(lines) == 1
    assert lines[0]["event"] == "sample_reserved"
    lines[0]["identity_digest"] = "0" * 64
    forged = json.dumps(lines[0], sort_keys=True, separators=(",", ":")).encode()
    journal.write_bytes(forged + b"\n")
    with pytest.raises(CockpitConflictError) as exc_info:
        request_sample(tmp_path, identity)
    assert exc_info.value.code == "sample-request-conflict"
