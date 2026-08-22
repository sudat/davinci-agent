"""Local reference ingestion — task 25."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.reference_learning.models import ReferenceLibraryV1, ReferenceSourceV1

if TYPE_CHECKING:
    from services.contracts.primitives import Producer


class ReferenceIngestError(Exception):
    """Base typed error for reference ingestion."""


class LocalReferenceUnavailable(ReferenceIngestError):  # noqa: N818
    """Typed error when a local reference file cannot be ingested."""


class _LocalIngestReturn(ReferenceSourceV1):
    """ReferenceSourceV1 that is also unpackable as (source, library)."""

    def __iter__(self) -> Iterator[ReferenceLibraryV1 | ReferenceSourceV1]:  # type: ignore[override]
        yield self
        yield object.__getattribute__(self, "_library")

    def __getitem__(self, index: int) -> ReferenceLibraryV1 | ReferenceSourceV1:  # type: ignore[override]
        if index == 0:
            return self
        if index == 1:
            return object.__getattribute__(self, "_library")
        raise IndexError(index)

    @property
    def library(self) -> ReferenceLibraryV1:
        return object.__getattribute__(self, "_library")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_local_reference(
    path: str | Path,
    *,
    library: ReferenceLibraryV1,
    source_id: str | None = None,
    provenance: Producer | None = None,  # type: ignore[name-defined]
    created_at: str | None = None,
) -> _LocalIngestReturn:
    """Ingest a local reference file.

    Local files are always accepted (existence + readable check only).
    Computes sha256 + size, registers path + hash only.
    """
    p = Path(path)

    if not p.exists() or not p.is_file():
        raise LocalReferenceUnavailable(f"Local reference not found: {p}")
    try:
        p.stat()
        with p.open("rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise LocalReferenceUnavailable(f"Local reference not readable: {p}") from exc

    sha = _compute_sha256(p)
    _ = p.stat().st_size

    resolved_source_id = source_id if source_id is not None else f"ref-{sha[:16]}"
    resolved_provenance = provenance if provenance is not None else library.provenance
    resolved_created_at = created_at if created_at is not None else _now_iso()

    source_plain = ReferenceSourceV1(
        source_id=resolved_source_id,
        kind="local_file",
        location=str(p),
        created_at=resolved_created_at,
        provenance=resolved_provenance,
        sha256=sha,
    )

    payload: dict[str, Any] = source_plain.model_dump(mode="json")
    ret = _LocalIngestReturn.model_validate(payload)

    new_library = library.model_copy(
        update={
            "version": library.version + 1,
            "sources": (*library.sources, ret),
        }
    )
    object.__setattr__(ret, "_library", new_library)
    return ret
