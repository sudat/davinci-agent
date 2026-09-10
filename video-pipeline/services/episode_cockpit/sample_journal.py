"""Sample journal append/recovery/idempotency (wave 1, P2-1 split b).

Journal-only reserve (no final dir at reserve time); the recovering
path re-verifies the published artifact and appends a missing success
line exactly once. Full content verification and budget-settle
completion live in sample_complete.py; both are re-exported through
consultation_samples.py for compatibility.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from services.episode_cockpit.consultation_store import consultation_write_locked
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SampleEventKind,
    SampleJournalEventV1,
    SampleManifestV1,
    SampleRequestIdentityV1,
    derive_sample_id,
    sample_dir,
    sample_identity_digest,
)
from services.foundation_io import canonical_model_bytes


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _journal_path(episode_dir: Path) -> Path:
    return episode_dir / "consultation" / "samples.jsonl"


def _append_event_locked(  # noqa: PLR0913 (journal-line contract: one field per record slot)
    episode_dir: Path,
    *,
    event: SampleEventKind,
    sample_id: str,
    digest: str,
    operation_id: str | None = None,
    sample_attempt_id: str | None = None,
    detail: str | None = None,
) -> SampleJournalEventV1:
    record = SampleJournalEventV1(
        event=event,
        sample_id=sample_id,
        identity_digest=digest,
        operation_id=operation_id,
        sample_attempt_id=sample_attempt_id,
        created_at=_now(),
        detail=detail,
    )
    path = _journal_path(episode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(record) + b"\n")
    return record


def _journal_events_locked(episode_dir: Path) -> list[SampleJournalEventV1]:
    path = _journal_path(episode_dir)
    if not path.exists():
        return []
    events: list[SampleJournalEventV1] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            events.append(SampleJournalEventV1.model_validate_json(line))
        except ValidationError as error:
            raise CockpitUnprocessableError(
                "sample-journal-corrupt",
                "試し動画の記録が壊れているため、要求を受け付けませんでした。",
            ) from error
    return events


def load_sample_events(episode_dir: Path) -> list[SampleJournalEventV1]:
    """Lock-free journal read (for status views and tests)."""
    with consultation_write_locked(episode_dir):
        return _journal_events_locked(episode_dir)


def sample_success_journaled_locked(episode_dir: Path, sample_id: str) -> bool:
    """True when a ``sample_success`` line exists; caller MUST hold the write lock."""
    return any(
        e.sample_id == sample_id and e.event == "sample_success"
        for e in _journal_events_locked(episode_dir)
    )


def complete_missing_success_locked(
    episode_dir: Path, sample_id: str, digest: str, identity: SampleRequestIdentityV1,
    sample_attempt_id: str | None,
) -> bool:
    """Append-once the missing success line (P1-3); True when appended."""
    if sample_success_journaled_locked(episode_dir, sample_id):
        return False
    _append_event_locked(
        episode_dir, event="sample_success", sample_id=sample_id, digest=digest,
        operation_id=identity.operation_id, sample_attempt_id=sample_attempt_id,
        detail="recovered: success journal line was missing after publish",
    )
    return True


def _reserved_payload(
    *, manifest: SampleManifestV1, sample_attempt_id: str, origin: str | None
) -> str:
    return json.dumps(
        {
            "manifest": manifest.model_dump(mode="json"),
            "origin_sample_attempt_id": origin,
            "sample_attempt_id": sample_attempt_id,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _reserved_manifest_from_detail(
    detail: str | None, identity: SampleRequestIdentityV1, sample_id: str
) -> SampleManifestV1:
    if detail is not None:
        try:
            return SampleManifestV1.model_validate(json.loads(detail)["manifest"])
        except (ValueError, KeyError, ValidationError):
            pass
    return SampleManifestV1(
        sample_id=sample_id, identity=identity, total_seconds=0.0,
        status="reserved", created_at=_now(), full_ir_sha256=identity.full_ir_sha256,
    )


def _fresh_reserve_locked(  # noqa: PLR0913 (reserve record: identity + journal scan slots)
    episode_dir: Path, identity: SampleRequestIdentityV1,
    sample_id: str, digest: str, events: list[SampleJournalEventV1], *, origin: str | None,
) -> dict[str, Any]:
    sequence = sum(1 for e in events if e.event == "sample_reserved") + 1
    sample_attempt_id = f"sample-attempt-{sequence}"
    manifest = SampleManifestV1(
        sample_id=sample_id, identity=identity, total_seconds=0.0,
        status="reserved", created_at=_now(), full_ir_sha256=identity.full_ir_sha256,
    )
    _append_event_locked(
        episode_dir, event="sample_reserved", sample_id=sample_id, digest=digest,
        operation_id=identity.operation_id, sample_attempt_id=sample_attempt_id,
        detail=_reserved_payload(
            manifest=manifest, sample_attempt_id=sample_attempt_id, origin=origin),
    )
    return {
        "state": "reserved", "manifest": manifest,
        "sample_attempt_id": sample_attempt_id, "origin_sample_attempt_id": origin,
    }


def request_sample_locked(
    episode_dir: Path, identity: SampleRequestIdentityV1
) -> dict[str, Any]:
    """Idempotent request; caller MUST hold ``consultation_write_locked``."""
    from services.episode_cockpit.sample_complete import (  # noqa: PLC0415 (lazy: completion owns verification)
        verify_recovery,
    )

    sample_id = derive_sample_id(identity)
    digest = sample_identity_digest(identity)
    events = _journal_events_locked(episode_dir)
    if any(
        e.operation_id == identity.operation_id and e.identity_digest != digest
        for e in events
    ):
        raise CockpitConflictError(
            "sample-request-conflict",
            "同じ操作IDで異なる内容の試し動画要求です。内容を確かめて再依頼してください。",
        )
    if (sample_dir(episode_dir, sample_id) / SAMPLE_MANIFEST_NAME).exists():
        manifest = verify_recovery(episode_dir, sample_id, identity)
        if sample_success_journaled_locked(episode_dir, sample_id):
            return {"state": "stored", "manifest": manifest}
        complete_missing_success_locked(
            episode_dir, sample_id, digest, identity, manifest.sample_attempt_id)
        return {"state": "recovering", "manifest": manifest}
    related = [e for e in events if e.sample_id == sample_id]
    open_reserve: SampleJournalEventV1 | None = None
    for entry in related:
        if entry.event == "sample_reserved":
            open_reserve = entry
        elif entry.event in ("sample_success", "sample_failed"):
            open_reserve = None
    if open_reserve is not None:
        if open_reserve.identity_digest != digest:
            raise CockpitConflictError(
                "sample-request-conflict",
                "同じ試し動画IDで異なる内容の試し動画要求です。"
                "内容を確かめて再依頼してください。",
            )
        return {
            "state": "reserved",
            "manifest": _reserved_manifest_from_detail(
                open_reserve.detail, identity, sample_id),
            "sample_attempt_id": open_reserve.sample_attempt_id,
            "origin_sample_attempt_id": None, "joined": True,
        }
    origin: str | None = None
    failed = [e for e in related if e.event in ("sample_success", "sample_failed")]
    if failed and failed[-1].event == "sample_failed":
        origin = failed[-1].sample_attempt_id or next(
            (e.sample_attempt_id for e in reversed(related) if e.sample_attempt_id),
            None,
        )
    return _fresh_reserve_locked(
        episode_dir, identity, sample_id, digest, events, origin=origin)


def request_sample(
    episode_dir: Path, identity: SampleRequestIdentityV1
) -> dict[str, Any]:
    """Public wrapper: the whole check + reserve runs atomically under the lock."""
    with consultation_write_locked(episode_dir):
        return request_sample_locked(episode_dir, identity)


def record_sample_success_locked(  # noqa: PLR0913 (success record: one field per journal slot)
    episode_dir: Path, *, sample_id: str, digest: str,
    operation_id: str | None = None, sample_attempt_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Append ``sample_success``; the caller MUST hold ``consultation_write_locked``."""
    _append_event_locked(
        episode_dir, event="sample_success", sample_id=sample_id, digest=digest,
        operation_id=operation_id, sample_attempt_id=sample_attempt_id, detail=detail,
    )


def record_sample_success(  # noqa: PLR0913 (same contract through the public wrapper)
    episode_dir: Path, *, sample_id: str, digest: str,
    operation_id: str | None = None, sample_attempt_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Public wrapper: the success append happens exactly once under the lock."""
    with consultation_write_locked(episode_dir):
        record_sample_success_locked(
            episode_dir, sample_id=sample_id, digest=digest,
            operation_id=operation_id, sample_attempt_id=sample_attempt_id, detail=detail,
        )


def settle_sample_failed_locked(
    episode_dir: Path, identity: SampleRequestIdentityV1, detail: str, *,
    sample_attempt_id: str | None = None,
) -> None:
    """Journal-only failure settle; the caller MUST hold ``consultation_write_locked``."""
    _append_event_locked(
        episode_dir, event="sample_failed", sample_id=derive_sample_id(identity),
        digest=sample_identity_digest(identity), operation_id=identity.operation_id,
        sample_attempt_id=sample_attempt_id, detail=detail,
    )


def settle_sample_failed(
    episode_dir: Path, identity: SampleRequestIdentityV1, detail: str, *,
    sample_attempt_id: str | None = None,
) -> None:
    """Public wrapper: the failure append happens exactly once under the lock."""
    with consultation_write_locked(episode_dir):
        settle_sample_failed_locked(
            episode_dir, identity, detail, sample_attempt_id=sample_attempt_id)
