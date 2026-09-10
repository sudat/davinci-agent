"""Sample completion: terminal manifest, recovery verification, settle.

Post-render completion half of the journal cluster (P2-1): the
PUBLISHED manifest writer (lineage/sha derived, never caller-claimed),
the full closed recovery verification (P1-5), and the append-once
budget-settle completion from manifest-persisted values (P1-3).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from services.compile.sample_projection import project_sample_ir
from services.episode_cockpit.consultation_selection_budget import (
    SelectionAttempt,
    load_selection_budget_entries,
    settle_preview,
)
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SampleManifestV1,
    SampleRequestIdentityV1,
    derive_sample_id,
    derive_sample_lineage,
    normalized_windows,
    sample_dir,
    sample_ir_sha256,
    verify_sample_content,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.validate.edit_commit_schema import tuplize

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C


def _fail_closed(detail: str) -> CockpitUnprocessableError:
    return CockpitUnprocessableError("sample-recovery-verification-failed", detail)


def _same_identity(manifest: SampleManifestV1, identity: SampleRequestIdentityV1) -> bool:
    if manifest.identity.model_copy(update={"windows": ()}) != identity.model_copy(
        update={"windows": ()}
    ):
        return False
    return normalized_windows(manifest.identity) == normalized_windows(identity)


def verify_recovery(
    episode_dir: Path, sample_id: str, identity: SampleRequestIdentityV1
) -> SampleManifestV1:
    """Full closed recovery verification (P1-5); caller MUST hold the write lock."""
    manifest = _verify_manifest_files(episode_dir, sample_id, identity)
    _verify_derivation(episode_dir, identity, manifest)
    _verify_cross_refs(episode_dir, sample_id, identity, manifest)
    return manifest


def _verify_manifest_files(
    episode_dir: Path, sample_id: str, identity: SampleRequestIdentityV1
) -> SampleManifestV1:
    manifest_path = sample_dir(episode_dir, sample_id) / SAMPLE_MANIFEST_NAME
    try:
        manifest = SampleManifestV1.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise _fail_closed("試し動画の記録が壊れているため、回復できませんでした。") from error
    video_path = sample_dir(episode_dir, sample_id) / SAMPLE_PREVIEW_NAME
    if not video_path.is_file():
        raise _fail_closed("試し動画の映像が無いため、回復できませんでした。")
    try:
        video_sha = sha256_file(video_path)
    except OSError as error:
        raise _fail_closed("試し動画の映像が読めないため、回復できませんでした。") from error
    if manifest.content_sha256 is None or video_sha != manifest.content_sha256:
        raise _fail_closed("試し動画の映像が記録と一致しないため、回復できませんでした。")
    if not _same_identity(manifest, identity):
        raise _fail_closed("試し動画の依頼内容が記録と一致しないため、回復できませんでした。")
    if manifest.full_ir_sha256 != identity.full_ir_sha256:
        raise _fail_closed("試し動画の元映像が変わっているため、回復できませんでした。")
    return manifest


def _verify_derivation(
    episode_dir: Path, identity: SampleRequestIdentityV1, manifest: SampleManifestV1
) -> None:
    from services.contracts.timeline_ir import TimelineIr0C  # noqa: PLC0415 (runtime parse only)

    ir_path = episode_dir / "review" / "store" / f"ir-{identity.base_version}.json"
    try:
        ir_bytes = ir_path.read_bytes()
    except OSError as error:
        raise _fail_closed("試し動画の元映像が読めないため、回復できませんでした。") from error
    if hashlib.sha256(ir_bytes).hexdigest() != identity.full_ir_sha256:
        raise _fail_closed("試し動画の元映像が変わっているため、回復できませんでした。")
    try:
        full_ir = TimelineIr0C.model_validate(tuplize(json.loads(ir_bytes)))
        sample_ir = project_sample_ir(full_ir, list(normalized_windows(identity)))
    except (ValidationError, ValueError, PydanticCustomError) as error:
        raise _fail_closed("試し動画の構成を確かめられないため、回復できませんでした。") from error
    verify_sample_content(sample_ir, manifest)


def _verify_cross_refs(
    episode_dir: Path, sample_id: str,
    identity: SampleRequestIdentityV1, manifest: SampleManifestV1,
) -> None:
    from services.episode_cockpit.sample_journal import (  # noqa: PLC0415 (lazy: journal owns the IO)
        _journal_events_locked,
    )

    if manifest.sample_attempt_id is None or not any(
        e.sample_id == sample_id
        and e.event == "sample_reserved"
        and e.sample_attempt_id == manifest.sample_attempt_id
        for e in _journal_events_locked(episode_dir)
    ):
        raise _fail_closed("試し動画の試行記録が一致しないため、回復できませんでした。")
    if manifest.budget_entry_id is None or not any(
        e.attempt_id == manifest.budget_entry_id
        and e.consultation_id == identity.consultation_id
        and e.judgment_id == identity.judgment_id
        for e in load_selection_budget_entries(episode_dir)
    ):
        raise _fail_closed("試し動画の予算記録が一致しないため、回復できませんでした。")
    if not manifest.run_id:
        raise _fail_closed("試し動画の実行記録が一致しないため、回復できませんでした。")


def has_preview_settle_locked(episode_dir: Path, attempt_id: str) -> bool:
    """True when the budget ledger already settles this attempt; lock held."""
    return any(
        e.attempt_id == attempt_id and e.phase == "preview_settled"
        for e in load_selection_budget_entries(episode_dir)
    )


def complete_missing_budget_settle_locked(
    episode_dir: Path, manifest: SampleManifestV1, identity: SampleRequestIdentityV1,
) -> bool:
    """Append-once the missing budget settle from manifest values (P1-3)."""
    attempt_id = manifest.budget_entry_id
    if attempt_id is None or has_preview_settle_locked(episode_dir, attempt_id):
        return False
    settle_preview(
        episode_dir,
        SelectionAttempt(
            attempt_id=attempt_id,
            consultation_id=identity.consultation_id,
            judgment_id=identity.judgment_id,
            reservation_sequence=manifest.budget_reservation_sequence or 1,
        ),
        preview_seconds=manifest.total_seconds,
        wall_elapsed=max(manifest.wall_seconds_used, 0.001), result="succeeded",
    )
    return True


def write_published_manifest(  # noqa: PLR0913 (terminal publish: identity + render evidence in one record)
    temp_dir: Path, identity: SampleRequestIdentityV1, *,
    sample_ir: TimelineIr0C, total_seconds: float, content_sha256: str,
    sample_attempt_id: str, budget_entry_id: str, budget_reservation_sequence: int,
    run_id: str, wall_seconds_used: float,
) -> SampleManifestV1:
    """Write the PUBLISHED manifest INSIDE the temp render dir (P1-5)."""
    manifest = SampleManifestV1(
        sample_id=derive_sample_id(identity), identity=identity,
        total_seconds=total_seconds, sample_ir_sha256=sample_ir_sha256(sample_ir),
        content_sha256=content_sha256, status="published", created_at=_now(),
        published_at=_now(), budget_entry_id=budget_entry_id,
        budget_reservation_sequence=budget_reservation_sequence, run_id=run_id,
        sample_attempt_id=sample_attempt_id, wall_seconds_used=wall_seconds_used,
        full_ir_sha256=identity.full_ir_sha256, lineage=derive_sample_lineage(sample_ir),
    )
    atomic_write(temp_dir / SAMPLE_MANIFEST_NAME, canonical_model_bytes(manifest))
    return manifest


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
