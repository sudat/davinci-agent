"""Local lineage registry over the immutable artifact store.

The registry keeps a single canonical-JSON index file per registry root.
Every register path re-derives entry fields from the store meta sidecars
and re-verifies object bytes, so the index can never invent or alter
artifact content: the store stays the sole truth."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError

from services.artifact_registry.models import (
    RegistryEntry,
    RegistryIndex,
    mint_index,
)
from services.artifact_registry.store_view import (
    StoreViewError,
    meta_path,
    object_path,
    object_stats,
    parse_meta_bytes,
    read_file_nofollow,
)
from services.contracts.primitives import ArtifactId, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes

if TYPE_CHECKING:
    from services.artifact_store.models import PublicationReceipt
    from services.artifact_store.store import ArtifactStore

INDEX_FILE_NAME = "registry-index.json"


class RegistryError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class LineageNode(StrictModel):
    depth: int = Field(ge=0, strict=True)
    entry: RegistryEntry


class MissingFinding(StrictModel):
    artifact_id: ArtifactId
    content_sha256: Sha256
    reason: Literal["object-absent", "size-mismatch"]


def entry_from_store(
    store: ArtifactStore,
    *,
    artifact_id: str,
    sequence: int,
    expected_sha256: str | None = None,
) -> RegistryEntry:
    raw = read_file_nofollow(meta_path(store.store_root, artifact_id))
    if raw is None:
        raise RegistryError("meta-absent", f"no store meta sidecar for {artifact_id}")
    try:
        intent = parse_meta_bytes(raw, expected_id=artifact_id)
    except StoreViewError as error:
        raise RegistryError(error.code, error.detail) from error
    content_sha256 = intent.envelope.content_hash
    if expected_sha256 is not None and content_sha256 != expected_sha256:
        raise RegistryError(
            "meta-mismatch",
            f"store meta hash {content_sha256} != receipt {expected_sha256}",
        )
    stats = object_stats(object_path(store.store_root, content_sha256))
    if stats is None:
        raise RegistryError(
            "object-absent",
            f"no object bytes in store for {content_sha256}",
        )
    size, digest = stats
    if digest != content_sha256:
        raise RegistryError(
            "content-hash-mismatch",
            f"object bytes hash {digest} != {content_sha256}",
        )
    return RegistryEntry(
        artifact_id=artifact_id,
        artifact_type=intent.envelope.artifact_type,
        schema_version=intent.envelope.schema_version,
        content_sha256=content_sha256,
        size=size,
        producer=intent.envelope.producer,
        inputs=intent.envelope.inputs,
        sequence=sequence,
    )


def _verify_node_bytes(store: ArtifactStore, entry: RegistryEntry) -> None:
    stats = object_stats(object_path(store.store_root, entry.content_sha256))
    if stats is None:
        raise RegistryError(
            "lineage-gap",
            f"parent artifact {entry.artifact_id} bytes are missing from the store",
        )
    _size, digest = stats
    if digest != entry.content_sha256:
        raise RegistryError(
            "lineage-gap",
            f"parent artifact {entry.artifact_id} object bytes hash drift",
        )


class ArtifactRegistry:
    def __init__(self, index_root: Path) -> None:
        self._root = index_root
        index_root.mkdir(parents=True, exist_ok=True)

    @property
    def index_root(self) -> Path:
        return self._root

    @property
    def index_path(self) -> Path:
        return self._root / INDEX_FILE_NAME

    def load(self) -> RegistryIndex:
        raw = read_file_nofollow(self.index_path)
        if raw is None:
            return mint_index({})
        try:
            return RegistryIndex.model_validate_json(raw)
        except ValidationError as error:
            raise RegistryError(
                "index-invalid",
                f"registry index rejected (seal/schema): {error}",
            ) from error

    def save(self, index: RegistryIndex) -> None:
        atomic_write(self.index_path, canonical_model_bytes(index))

    def register(self, store: ArtifactStore, receipt: PublicationReceipt) -> RegistryEntry:
        index = self.load()
        existing = index.entries.get(receipt.artifact_id)
        if existing is not None:
            if existing.content_sha256 != receipt.content_sha256:
                raise RegistryError(
                    "registry-conflict",
                    f"artifact id {receipt.artifact_id} is already indexed "
                    f"with content {existing.content_sha256}",
                )
            return existing
        next_sequence = max(
            (entry.sequence for entry in index.entries.values()), default=-1
        ) + 1
        entry = entry_from_store(
            store,
            artifact_id=receipt.artifact_id,
            sequence=next_sequence,
            expected_sha256=receipt.content_sha256,
        )
        self.save(mint_index(index.entries | {entry.artifact_id: entry}))
        return entry

    def walk(
        self,
        store: ArtifactStore,
        artifact_id: ArtifactId,
        *,
        depth: int,
    ) -> tuple[LineageNode, ...]:
        if depth < 0:
            raise RegistryError("walk-depth", f"depth must be >= 0, got {depth}")
        index = self.load()
        nodes: list[LineageNode] = []
        on_path: set[str] = set()
        stack: list[tuple[str, int, bool]] = [(artifact_id, 0, True)]
        while stack:
            current_id, current_depth, enter = stack.pop()
            if not enter:
                on_path.discard(current_id)
                continue
            if current_id in on_path:
                raise RegistryError(
                    "lineage-cycle",
                    f"lineage cycle detected at {current_id}",
                )
            entry = index.entries.get(current_id)
            if entry is None:
                raise RegistryError(
                    "lineage-gap",
                    f"parent artifact {current_id} is missing from the index",
                )
            _verify_node_bytes(store, entry)
            nodes.append(LineageNode(depth=current_depth, entry=entry))
            stack.append((current_id, current_depth, False))
            if current_depth < depth:
                on_path.add(current_id)
                stack.extend(
                    (parent.artifact_id, current_depth + 1, True)
                    for parent in reversed(entry.inputs)
                )
        return tuple(nodes)

    def detect_missing(self, store: ArtifactStore) -> tuple[MissingFinding, ...]:
        index = self.load()
        findings: list[MissingFinding] = []
        for entry in sorted(
            index.entries.values(), key=lambda item: (item.sequence, item.artifact_id)
        ):
            stats = object_stats(object_path(store.store_root, entry.content_sha256))
            reason: Literal["object-absent", "size-mismatch"] | None = None
            if stats is None:
                reason = "object-absent"
            elif stats[0] != entry.size:
                reason = "size-mismatch"
            if reason is not None:
                findings.append(
                    MissingFinding(
                        artifact_id=entry.artifact_id,
                        content_sha256=entry.content_sha256,
                        reason=reason,
                    )
                )
        return tuple(findings)
