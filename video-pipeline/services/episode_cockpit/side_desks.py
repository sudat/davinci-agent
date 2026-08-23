"""Side desks: approvals read/append and local reference registration.

Approvals: the append-only OperationRecordStore is the only store touched;
an HTTP-sourced decision can only ever mint an automation-class
fixture-marked record (``record_fixture_operation``) — operator-grade
records require the controlling-TTY ingress by contract and cannot be
minted over HTTP. References: the real
``services.reference_learning.ingest`` call over one library file at the
episodes root; registration is idempotent by CONTENT hash (the same
bytes return the existing source instead of duplicating, task 51) and
the persisted library lists read-only over GET /references.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.contracts.primitives import Producer
from services.episode_cockpit.errors import CockpitNotFoundError, CockpitUnprocessableError
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.foundation_io import atomic_write, canonical_model_bytes
from services.reference_learning.ingest import (
    LocalReferenceUnavailable,
    ingest_local_reference,
)
from services.reference_learning.models import ReferenceLibraryV1

APPROVALS_RELATIVE = ("approvals", "records.jsonl")
LIBRARY_NAME = "reference-library.json"


class ApprovalOps(WorkspaceContext):
    """Purpose-bound approval records, read + automation-class append."""

    def list_approvals(self, episode_id: str) -> dict[str, object]:
        episode_dir = self._require_snapshot(episode_id).job.episode_id
        records = OperationRecordStore(
            self._episode_dir(episode_dir).joinpath(*APPROVALS_RELATIVE)
        ).all_records()
        return {
            "available": bool(records),
            "approvals": [
                {
                    "record_id": record.record_id,
                    "purpose": record.purpose,
                    "target_type": record.target_type,
                    "target_hash": record.target_hash,
                    "decision": record.decision,
                    "actor_id": record.actor_id,
                    "timestamp_seq": record.timestamp_seq,
                }
                for record in records
            ],
        }

    def execute_approval(
        self, episode_id: str, approval_id: str, *, decision: str, actor_id: str
    ) -> dict[str, object]:
        episode_dir = self._require_snapshot(episode_id).job.episode_id
        store = OperationRecordStore(
            self._episode_dir(episode_dir).joinpath(*APPROVALS_RELATIVE)
        )
        target = next((r for r in store.all_records() if r.record_id == approval_id), None)
        if target is None:
            raise CockpitNotFoundError(
                "approval-not-found", f"no approval record {approval_id}"
            )
        draft = record_fixture_operation(
            purpose=target.purpose,
            target_bundle_hash=target.target_hash,
            decision=decision,  # type: ignore[arg-type]  # Literal narrowed by request model
            actor_id=actor_id,
        )
        appended = store.append(draft)
        return {
            "record_id": appended.record_id,
            "decision": appended.decision,
            "superseded_record_id": appended.superseded_record_id,
            "runner_class": appended.runner_class,
            "fixture_only": appended.fixture_only,
        }


class ReferenceOps(WorkspaceContext):
    """Local reference registration into the shared reference library."""

    def register_reference(self, *, path: str, source_id: str | None) -> dict[str, object]:
        library = self._load_library()
        try:
            content_sha = _file_sha256(Path(path))
        except OSError as error:
            raise CockpitUnprocessableError("reference-unavailable", str(error)) from error
        existing = next(
            (source for source in library.sources if source.sha256 == content_sha), None
        )
        if existing is not None:
            return {
                "source_id": existing.source_id,
                "sha256": existing.sha256,
                "library_version": library.version,
                "idempotent": True,
            }
        try:
            source, new_library = ingest_local_reference(
                path, library=library, source_id=source_id
            )
        except LocalReferenceUnavailable as error:
            raise CockpitUnprocessableError("reference-unavailable", str(error)) from error
        atomic_write(
            self._episodes_root / LIBRARY_NAME, canonical_model_bytes(new_library)
        )
        return {
            "source_id": source.source_id,
            "sha256": source.sha256,
            "library_version": new_library.version,
            "idempotent": False,
        }

    def list_references(self) -> dict[str, object]:
        """Read-only persisted-library listing (task 51 GET /references)."""

        path: Path = self._episodes_root / LIBRARY_NAME
        if not path.is_file():
            return {"available": False, "references": []}
        library = ReferenceLibraryV1.model_validate_json(path.read_bytes())
        return {
            "available": bool(library.sources),
            "references": [
                {
                    "source_id": source.source_id,
                    "kind": source.kind,
                    "location": source.location,
                    "sha256": source.sha256,
                    "created_at": source.created_at,
                }
                for source in library.sources
            ],
        }

    def _load_library(self) -> ReferenceLibraryV1:
        path: Path = self._episodes_root / LIBRARY_NAME
        if not path.is_file():
            return ReferenceLibraryV1(
                library_id="reflib-default",
                version=1,
                provenance=Producer(name="episode-cockpit", version="1"),
                created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        return ReferenceLibraryV1.model_validate_json(path.read_bytes())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ApprovalOps", "ReferenceOps"]
