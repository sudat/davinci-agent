"""Sample identity, manifest, and journal models (wave 1, P2-1 split a).

Pure data + digests. No journal IO, no locks, no rendering. The journal
append/recovery/idempotency layer lives in sample_journal.py; both are
re-exported through consultation_samples.py for compatibility.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BeforeValidator

from services.contracts.primitives import (
    Identifier,
    ItemId,
    RecordFrameSpan,
    Sha256,
    StrictModel,
)
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.foundation_io import canonical_model_bytes
from services.outputs.geometry import (
    OutputId,  # noqa: TC001 (pydantic resolves the Literal at model-build time)
)

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C

SAMPLES_DIR_NAME = "samples"
SAMPLES_JOURNAL_NAME = "samples.jsonl"
SAMPLE_PREVIEW_NAME = "preview.mp4"
SAMPLE_MANIFEST_NAME = "manifest.json"
SAMPLE_RENDER_LOCK_NAME = "render.lock"

type SampleEventKind = Literal["sample_reserved", "sample_success", "sample_failed"]
type SampleStatus = Literal["reserved", "published"]

type WindowSequence = Annotated[tuple[RecordFrameSpan, ...], BeforeValidator(tuple)]


class SampleRequestIdentityV1(StrictModel):
    episode_id: Identifier
    consultation_id: Identifier
    judgment_id: Identifier
    base_version: Identifier
    base_plan_sha256: Sha256
    policy_sha256: Sha256 | None
    output_id: OutputId
    windows: WindowSequence
    operation_id: Identifier
    full_ir_sha256: Sha256


class SampleLineageEntryV1(StrictModel):
    source_item_id: ItemId
    sample_item_id: ItemId


type LineageSequence = Annotated[
    tuple[SampleLineageEntryV1, ...], BeforeValidator(tuple)
]


class SampleManifestV1(StrictModel):
    sample_id: Identifier
    identity: SampleRequestIdentityV1
    total_seconds: float
    sample_ir_sha256: Sha256 | None = None
    content_sha256: Sha256 | None = None
    status: SampleStatus
    created_at: str
    published_at: str | None = None
    budget_entry_id: Identifier | None = None
    budget_reservation_sequence: int | None = None
    run_id: Identifier | None = None
    sample_attempt_id: Identifier | None = None
    wall_seconds_used: float = 0.0
    full_ir_sha256: Sha256 | None = None
    lineage: LineageSequence = ()


class SampleJournalEventV1(StrictModel):
    event: SampleEventKind
    sample_id: Identifier
    identity_digest: Sha256
    operation_id: Identifier | None = None
    sample_attempt_id: Identifier | None = None
    created_at: str
    detail: str | None = None


def normalized_windows(
    identity: SampleRequestIdentityV1,
) -> tuple[RecordFrameSpan, ...]:
    """Canonical window order: a reorder-only difference is the same request."""
    return tuple(sorted(identity.windows, key=lambda w: (w.start_frame, w.end_frame)))


def sample_identity_digest(identity: SampleRequestIdentityV1) -> str:
    """Identity digest over NORMALIZED windows (no double charge on reorder)."""
    normalized = identity.model_copy(update={"windows": normalized_windows(identity)})
    return hashlib.sha256(canonical_model_bytes(normalized)).hexdigest()


def derive_sample_id(identity: SampleRequestIdentityV1) -> str:
    """Short sample id: ``sample-`` + the identity digest's first 16 hex.

    The truncation is collision-reject-safe, not collision-proof: the
    FULL digest rides every journal line (``identity_digest``) and the
    manifest pins the full identity, so a prefix collision can never
    join another request's reserve (``request_sample_locked`` refuses a
    digest mismatch as ``sample-request-conflict``) nor recover its
    directory (``verify_recovery`` compares the full identity). A
    colliding request fails closed instead of sharing an artifact.
    """
    return f"sample-{sample_identity_digest(identity)[:16]}"


def samples_root(episode_dir: Path) -> Path:
    return episode_dir / "consultation" / SAMPLES_DIR_NAME


def sample_dir(episode_dir: Path, sample_id: str) -> Path:
    return samples_root(episode_dir) / sample_id


def render_lock_path(episode_dir: Path, sample_id: str) -> Path:
    """Per-sample render-owner lock (P1-1: exactly one renderer per attempt)."""
    return samples_root(episode_dir) / f"{sample_id}.{SAMPLE_RENDER_LOCK_NAME}"


def episode_render_lock_path(episode_dir: Path) -> Path:
    """Episode-wide sample-render lock (serialized sample renders).

    Persistent file, never unlinked: blocking flock on one stable inode
    is what serializes owners across processes.
    """
    return samples_root(episode_dir) / SAMPLE_RENDER_LOCK_NAME


def derive_sample_lineage(sample_ir: TimelineIr0C) -> tuple[SampleLineageEntryV1, ...]:
    """Lineage DERIVED from the projected IR (P1-5: never caller-claimed).

    The projection names each clip ``{source}.s{window}``; stripping the
    suffix recovers the source item. Deterministic in IR order.
    """
    entries: list[SampleLineageEntryV1] = []
    for track in sample_ir.tracks:
        entries.extend(
            SampleLineageEntryV1(
                source_item_id=re.sub(r"\.s\d+$", "", item.item_id),
                sample_item_id=item.item_id,
            )
            for item in track.items
        )
    return tuple(entries)


def sample_ir_sha256(sample_ir: TimelineIr0C) -> str:
    return hashlib.sha256(canonical_model_bytes(sample_ir)).hexdigest()


def verify_sample_content(sample_ir: TimelineIr0C, manifest: SampleManifestV1) -> None:
    """Re-derived IR check (P1-5): sha + lineage must match the manifest.

    ANY mismatch is a typed stop — never a success completion.
    """
    if manifest.sample_ir_sha256 != sample_ir_sha256(sample_ir):
        raise CockpitUnprocessableError(
            "sample-recovery-verification-failed",
            "試し動画の構成が記録と一致しないため、回復できませんでした。",
        )
    if tuple(manifest.lineage) != derive_sample_lineage(sample_ir):
        raise CockpitUnprocessableError(
            "sample-recovery-verification-failed",
            "試し動画の来歴が記録と一致しないため、回復できませんでした。",
        )


__all__: list[str] = [
    "SAMPLES_DIR_NAME",
    "SAMPLES_JOURNAL_NAME",
    "SAMPLE_MANIFEST_NAME",
    "SAMPLE_PREVIEW_NAME",
    "SAMPLE_RENDER_LOCK_NAME",
    "LineageSequence",
    "SampleEventKind",
    "SampleJournalEventV1",
    "SampleLineageEntryV1",
    "SampleManifestV1",
    "SampleRequestIdentityV1",
    "SampleStatus",
    "WindowSequence",
    "derive_sample_id",
    "derive_sample_lineage",
    "episode_render_lock_path",
    "normalized_windows",
    "render_lock_path",
    "sample_dir",
    "sample_identity_digest",
    "sample_ir_sha256",
    "samples_root",
    "verify_sample_content",
]
