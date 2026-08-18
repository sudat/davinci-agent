"""The typed review bundle: the H1 operator's view of one review lineage.

A review bundle binds, for one PREVIEW_READY episode, the immutable review
store (Todo 30), the current committed review-plane plan/IR/preview hashes,
the synthesized edit-source media world, and the policy hashes the checkpoint
must rehash. Every path inside the bundle is RELATIVE to the bundle file so
the whole directory can be moved without silent drift; every hash is
recomputable from the referenced bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

BUNDLE_NAME = "review-bundle.json"
PREVIEW_FILE = "preview.mp4"
TRACE_FILE = "preview-trace.json"


class MediaFileInfo(StrictModel):
    role: str
    path: str
    sha256: Sha256


class ReviewTarget(StrictModel):
    plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    plan_sha256: Sha256
    ir_sha256: Sha256
    preview_dir: str
    preview_sha256: Sha256
    trace_sha256: Sha256


class ReviewBundle(StrictModel):
    schema_version: Literal["review-bundle-v1"]
    episode_id: Identifier
    fixture_only: bool
    stage: Literal["PREVIEW_READY"]
    eligibility_status: Literal["supported", "assisted", "unsupported"]
    store_dir: str
    events_log: str
    media: tuple[MediaFileInfo, ...] = Field(min_length=1)
    edit_source_world_sha256: Sha256
    fixture_manifest_sha256: Sha256
    toolchain_lock_sha256: Sha256
    translator_policy_sha256: Sha256
    production_policy_sha256: Sha256 | None = None
    fixture_manifest_path: str = "manifest.json"
    initial: ReviewTarget
    current: ReviewTarget
    applied_event_ids: tuple[Sha256, ...] = ()

    @model_validator(mode="after")
    def require_supported_episode(self) -> ReviewBundle:
        if self.eligibility_status == "unsupported":
            raise PydanticCustomError(
                "unsupported_episode", "unsupported episodes never reach PREVIEW_READY"
            )
        return self


class BundleDriftError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def bundle_path(bundle_dir: Path) -> Path:
    return bundle_dir / BUNDLE_NAME


def save_bundle(bundle: ReviewBundle, path: Path) -> None:
    atomic_write(path, canonical_model_bytes(bundle))


def load_bundle(path: Path) -> ReviewBundle:
    try:
        return ReviewBundle.model_validate_json(path.read_bytes())
    except OSError as error:
        raise BundleDriftError("bundle_unreadable", str(error)) from error


def resolve_path(bundle_file: Path, relative: str) -> Path:
    return (bundle_file.parent / relative).resolve()


def _require_sha(path: Path, expected: Sha256, label: str) -> None:
    if not path.is_file():
        raise BundleDriftError("target_missing", f"{label} is missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise BundleDriftError(
            "target_drift",
            f"{label} hashes {actual[:12]} but the bundle displays {expected[:12]}",
        )


def verify_media(bundle: ReviewBundle, bundle_file: Path) -> None:
    for media in bundle.media:
        _require_sha(resolve_path(bundle_file, media.path), media.sha256, f"media {media.role}")


def rehash_bundle_targets(bundle: ReviewBundle, bundle_file: Path) -> None:
    """Rehash every displayed target; refuse on drift (checkpoint precondition)."""

    verify_media(bundle, bundle_file)
    store_dir = bundle_file.parent / bundle.store_dir
    for target in (bundle.initial, bundle.current):
        preview_dir = bundle_file.parent / target.preview_dir
        _require_sha(preview_dir / PREVIEW_FILE, target.preview_sha256, "preview")
        _require_sha(preview_dir / TRACE_FILE, target.trace_sha256, "preview trace")
        version = target.plan_version[1:]
        _require_sha(
            store_dir / f"plan-v{version}.json", target.plan_sha256, f"plan {target.plan_version}"
        )
        _require_sha(
            store_dir / f"ir-v{version}.json", target.ir_sha256, f"IR {target.plan_version}"
        )


def assemble_initial_bundle(  # noqa: PLR0913 (bundle fields are the H1 contract)
    manifest: Phase1TechnicalFixtureManifest,
    *,
    eligibility_status: Literal["supported", "assisted", "unsupported"],
    mezzanine_sha256: str,
    edit_source_world_sha256: str,
    manifest_sha256: str,
    target: ReviewTarget,
) -> ReviewBundle:
    """Assemble the PREVIEW_READY review bundle for a freshly run episode."""

    return ReviewBundle(
        schema_version="review-bundle-v1",
        episode_id=manifest.fixture_id,
        fixture_only=manifest.fixture_only,
        stage="PREVIEW_READY",
        eligibility_status=eligibility_status,
        store_dir="review-store",
        events_log="review-store/events.jsonl",
        media=(
            MediaFileInfo(
                role="edit-source",
                path="media/edit-source.mov",
                sha256=mezzanine_sha256,
            ),
        ),
        edit_source_world_sha256=edit_source_world_sha256,
        fixture_manifest_sha256=manifest_sha256,
        toolchain_lock_sha256=sha256_file(Path("config/toolchains/phase-1-technical-v1.json")),
        translator_policy_sha256=sha256_file(Path("config/gates/phase-0c-v1.json")),
        initial=target,
        current=target,
        applied_event_ids=(),
    )


def assemble_real_bundle(  # noqa: PLR0913 (bundle fields are the H1 contract)
    *,
    episode_id: str,
    eligibility_status: Literal["supported", "assisted", "unsupported"],
    mezzanine_sha256: str,
    edit_source_world_sha256: str,
    episode_manifest_sha256: str,
    policy_sha256: str,
    target: ReviewTarget,
) -> ReviewBundle:
    """Assemble the PREVIEW_READY review bundle for a freshly run REAL episode."""

    return ReviewBundle(
        schema_version="review-bundle-v1",
        episode_id=episode_id,
        fixture_only=False,
        stage="PREVIEW_READY",
        eligibility_status=eligibility_status,
        store_dir="review-store",
        events_log="review-store/events.jsonl",
        media=(
            MediaFileInfo(
                role="edit-source",
                path="media/edit-source.mov",
                sha256=mezzanine_sha256,
            ),
        ),
        edit_source_world_sha256=edit_source_world_sha256,
        fixture_manifest_sha256=episode_manifest_sha256,
        toolchain_lock_sha256=sha256_file(Path("config/toolchains/phase-1-technical-v1.json")),
        translator_policy_sha256=sha256_file(Path("config/gates/phase-0c-v1.json")),
        production_policy_sha256=policy_sha256,
        fixture_manifest_path="episode.json",
        initial=target,
        current=target,
        applied_event_ids=(),
    )


__all__ = [
    "BUNDLE_NAME",
    "PREVIEW_FILE",
    "TRACE_FILE",
    "BundleDriftError",
    "MediaFileInfo",
    "ReviewBundle",
    "ReviewTarget",
    "assemble_initial_bundle",
    "assemble_real_bundle",
    "bundle_path",
    "load_bundle",
    "rehash_bundle_targets",
    "resolve_path",
    "save_bundle",
    "verify_media",
]
