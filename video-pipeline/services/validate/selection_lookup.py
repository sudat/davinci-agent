"""Committed-only Selection Plan lookup (Todo 41).

Downstream consumers may read ONLY committed plan versions — from the
versions index, from the artifact store, or from a file. A proposal
artifact id, a proposal file path, or an unknown version is refused
with an explicit committed-only error; proposals are never downstream
inputs in any form.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from services.artifact_registry.store_view import (
    StoreViewError,
    meta_path,
    parse_meta_bytes,
    read_file_nofollow,
)
from services.validate.selection_models import CommittedSelectionPlan
from services.validate.selection_plan_store import (
    latest_version,
    load_index,
)
from services.validate.selection_publisher import (
    COMMITTED_ARTIFACT_TYPE,
    PROPOSAL_ARTIFACT_TYPE,
)
from services.validate.selection_schema import PROPOSAL_SCHEMA_VERSION, tuplize

if TYPE_CHECKING:
    from services.artifact_store.store import ArtifactStore


class SelectionLookupError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CommittedPlanView:
    """A served committed plan: identity, content hash, and exact bytes."""

    plan_version: int
    episode_id: str
    plan_artifact_id: str
    content_sha256: str
    payload: bytes


def get_committed_plan(
    artifact_store: ArtifactStore, plan_dir: Path, *, version: int | None = None
) -> CommittedPlanView:
    """Serve a committed version (latest when version is None), bytes verified."""

    index = load_index(plan_dir)
    target = latest_version(index) if version is None else version
    if target < 1:
        raise SelectionLookupError(
            "no_committed_version", "no selection plan version has been committed yet"
        )
    entry = index.versions.get(str(target))
    if entry is None:
        raise SelectionLookupError(
            "unknown_version", f"version v{target} is not in the committed versions index"
        )
    payload, _ref = artifact_store.reopen(entry.plan_sha256)
    if hashlib.sha256(payload).hexdigest() != entry.plan_sha256:
        raise SelectionLookupError(
            "content_hash_mismatch", f"version v{target} bytes do not match the index hash"
        )
    return CommittedPlanView(
        plan_version=target,
        episode_id=index.episode_id,
        plan_artifact_id=entry.plan_artifact_id,
        content_sha256=entry.plan_sha256,
        payload=payload,
    )


def get_committed_plan_by_artifact_id(
    artifact_store: ArtifactStore, artifact_id: str
) -> CommittedPlanView:
    """Serve a committed plan by store artifact id; proposal artifacts refused."""

    raw = read_file_nofollow(meta_path(artifact_store.store_root, artifact_id))
    if raw is None:
        raise SelectionLookupError(
            "unknown_artifact", f"artifact {artifact_id} is not in the artifact store"
        )
    try:
        intent = parse_meta_bytes(raw, expected_id=artifact_id)
    except StoreViewError as error:
        raise SelectionLookupError(error.code, error.detail) from error
    artifact_type = intent.envelope.artifact_type
    if artifact_type == PROPOSAL_ARTIFACT_TYPE:
        raise SelectionLookupError(
            "committed_only",
            f"artifact {artifact_id} is a selection plan PROPOSAL; downstream reads"
            " accept committed plan versions only",
        )
    if artifact_type != COMMITTED_ARTIFACT_TYPE:
        raise SelectionLookupError(
            "not_a_selection_plan",
            f"artifact {artifact_id} is a {artifact_type}, not a committed selection plan",
        )
    payload, _ref = artifact_store.reopen(intent.envelope.content_hash)
    document: object = json.loads(payload)
    try:
        plan = CommittedSelectionPlan.model_validate(tuplize(document))
    except ValidationError as error:
        raise SelectionLookupError(
            "foreign_plan", f"artifact {artifact_id} bytes are not a committed plan: {error}"
        ) from error
    return CommittedPlanView(
        plan_version=int(plan.plan_version[1:]),
        episode_id=plan.episode_id,
        plan_artifact_id=artifact_id,
        content_sha256=intent.envelope.content_hash,
        payload=payload,
    )


def _looks_like_proposal(document: object) -> bool:
    return isinstance(document, dict) and (
        document.get("schema_version") == PROPOSAL_SCHEMA_VERSION
        or ("proposal_id" in document and "plan_version" not in document)
    )


def load_committed_plan_file(path: Path) -> CommittedSelectionPlan:
    """Refuse proposal FILES: a path is never an acceptable downstream source."""

    try:
        document: object = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise SelectionLookupError("unreadable_plan_file", str(error)) from error
    if _looks_like_proposal(document):
        raise SelectionLookupError(
            "committed_only",
            f"{path.name} is a selection plan PROPOSAL file; proposals are never"
            " downstream inputs",
        )
    try:
        return CommittedSelectionPlan.model_validate(tuplize(document))
    except ValidationError as error:
        raise SelectionLookupError(
            "not_a_committed_plan", f"{path.name} is not a committed plan: {error}"
        ) from error


__all__ = [
    "CommittedPlanView",
    "SelectionLookupError",
    "get_committed_plan",
    "get_committed_plan_by_artifact_id",
    "load_committed_plan_file",
]
