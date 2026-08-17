"""Orphan reconciliation and drift detection for the lineage registry.

Direction (a): store meta sidecars not present in the index are adopted
(orphan-recovered, journaled). Direction (b): index entries whose store
objects vanished are reported missing, never silently dropped. Corruption
guard: every indexed hash is recomputed against store truth; a mismatch
raises a registry-drift error while the immutable store bytes stay
untouched, and ``rebuild_index_from_store`` reconstructs the truthful
index from the store alone."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError

from services.artifact_registry.models import RegistryEntry, RegistryIndex, mint_index
from services.artifact_registry.registry import (
    ArtifactRegistry,
    MissingFinding,
    entry_from_store,
)
from services.artifact_registry.store_view import (
    ARTIFACTS_DIR_NAME,
    OBJECTS_DIR_NAME,
    is_object_name,
    meta_path,
    object_path,
    object_stats,
    read_file_nofollow,
)
from services.artifact_store.models import PublicationIntent
from services.contracts.primitives import ArtifactId, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.artifact_store.store import ArtifactStore

JOURNAL_FILE_NAME = "registry-journal.jsonl"
DriftReason = Literal[
    "meta-absent",
    "meta-invalid",
    "meta-hash-mismatch",
    "meta-field-mismatch",
    "object-hash-mismatch",
]


class DriftFinding(StrictModel):
    artifact_id: ArtifactId
    indexed_sha256: Sha256
    reason: DriftReason


class RegistryDriftError(Exception):
    def __init__(self, findings: tuple[DriftFinding, ...]) -> None:
        detail = ", ".join(
            f"{finding.artifact_id}:{finding.reason}" for finding in findings
        )
        super().__init__(f"registry-drift: {detail}")
        self.findings = findings


class ReconcileReport(StrictModel):
    adopted: tuple[RegistryEntry, ...] = ()
    missing: tuple[MissingFinding, ...] = ()
    unadopted_object_sha256s: tuple[Sha256, ...] = ()


class ReconcileJournalEvent(StrictModel):
    event: Literal["reconcile"] = "reconcile"
    adopted_ids: tuple[ArtifactId, ...] = ()
    unadopted_object_sha256s: tuple[Sha256, ...] = ()


class RebuildJournalEvent(StrictModel):
    event: Literal["index-rebuilt"] = "index-rebuilt"
    entry_count: int = Field(ge=0, strict=True)


JournalEvent = ReconcileJournalEvent | RebuildJournalEvent


def _append_journal(registry: ArtifactRegistry, event: JournalEvent) -> None:
    path = registry.index_root / JOURNAL_FILE_NAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, canonical_model_bytes(event) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def drift_findings(store: ArtifactStore, index: RegistryIndex) -> tuple[DriftFinding, ...]:
    findings: list[DriftFinding] = []
    for entry in sorted(
        index.entries.values(), key=lambda item: (item.sequence, item.artifact_id)
    ):
        reason = _entry_drift_reason(store, entry)
        if reason is not None:
            findings.append(
                DriftFinding(
                    artifact_id=entry.artifact_id,
                    indexed_sha256=entry.content_sha256,
                    reason=reason,
                )
            )
    return tuple(findings)


def _entry_drift_reason(store: ArtifactStore, entry: RegistryEntry) -> DriftReason | None:
    raw = read_file_nofollow(meta_path(store.store_root, entry.artifact_id))
    if raw is None:
        return "meta-absent"
    try:
        intent = PublicationIntent.model_validate_json(raw)
    except ValidationError:
        return "meta-invalid"
    envelope = intent.envelope
    if envelope.content_hash != entry.content_sha256:
        return "meta-hash-mismatch"
    if (
        envelope.artifact_type != entry.artifact_type
        or envelope.schema_version != entry.schema_version
        or envelope.producer != entry.producer
        or envelope.inputs != entry.inputs
    ):
        return "meta-field-mismatch"
    stats = object_stats(object_path(store.store_root, entry.content_sha256))
    if stats is not None and stats[1] != entry.content_sha256:
        return "object-hash-mismatch"
    return None


def verify_against_store(store: ArtifactStore, index: RegistryIndex) -> None:
    findings = drift_findings(store, index)
    if findings:
        raise RegistryDriftError(findings)


def _sorted_meta_paths(store: ArtifactStore) -> list[Path]:
    artifacts_dir = store.store_root / ARTIFACTS_DIR_NAME
    if not artifacts_dir.is_dir():
        return []
    return sorted(path for path in artifacts_dir.iterdir() if path.suffix == ".json")


def _unadopted_object_hashes(
    store: ArtifactStore, known_hashes: set[str]
) -> tuple[str, ...]:
    objects_dir = store.store_root / OBJECTS_DIR_NAME
    if not objects_dir.is_dir():
        return ()
    unadopted = [
        object_file.name
        for shard_dir in sorted(path for path in objects_dir.iterdir() if path.is_dir())
        for object_file in sorted(shard_dir.iterdir())
        if is_object_name(object_file.name) and object_file.name not in known_hashes
    ]
    return tuple(sorted(unadopted))


def reconcile(store: ArtifactStore, registry: ArtifactRegistry) -> ReconcileReport:
    index = registry.load()
    verify_against_store(store, index)
    missing = registry.detect_missing(store)
    adopted: list[RegistryEntry] = []
    for meta_file in _sorted_meta_paths(store):
        artifact_id = meta_file.stem
        if artifact_id in index.entries:
            continue
        adopted.append(
            entry_from_store(
                store,
                artifact_id=artifact_id,
                sequence=len(index.entries) + len(adopted),
            )
        )
    known_hashes = set(index.by_sha256) | {
        entry.content_sha256 for entry in adopted
    }
    unadopted = _unadopted_object_hashes(store, known_hashes)
    if adopted:
        entries = index.entries | {entry.artifact_id: entry for entry in adopted}
        registry.save(mint_index(entries))
    if adopted or unadopted:
        _append_journal(
            registry,
            ReconcileJournalEvent(
                adopted_ids=tuple(entry.artifact_id for entry in adopted),
                unadopted_object_sha256s=tuple(unadopted),
            ),
        )
    return ReconcileReport(
        adopted=tuple(adopted),
        missing=missing,
        unadopted_object_sha256s=tuple(unadopted),
    )


def rebuild_index_from_store(
    store: ArtifactStore, registry: ArtifactRegistry
) -> RegistryIndex:
    entries: dict[str, RegistryEntry] = {}
    for sequence, meta_file in enumerate(_sorted_meta_paths(store)):
        entry = entry_from_store(store, artifact_id=meta_file.stem, sequence=sequence)
        entries[entry.artifact_id] = entry
    index = mint_index(entries)
    registry.save(index)
    _append_journal(registry, RebuildJournalEvent(entry_count=len(entries)))
    return index


__all__ = [
    "JOURNAL_FILE_NAME",
    "DriftFinding",
    "DriftReason",
    "JournalEvent",
    "RebuildJournalEvent",
    "ReconcileJournalEvent",
    "ReconcileReport",
    "RegistryDriftError",
    "rebuild_index_from_store",
    "reconcile",
    "verify_against_store",
]
