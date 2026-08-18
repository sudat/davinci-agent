"""Committed-only Edit Plan lookup (Todo 43).

Downstream consumers may read ONLY committed edit plan versions — from the
edit versions index, from the artifact store, or from a file. An edit plan
PROPOSAL artifact id or file, a selection-plan artifact, or an unknown
version is refused with an explicit typed error; proposals are never
downstream inputs in any form.
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
from services.validate.edit_commit_models import CommittedEditPlan
from services.validate.edit_commit_publisher import (
    EDIT_COMMITTED_ARTIFACT_TYPE,
    EDIT_PROPOSAL_ARTIFACT_TYPE,
)
from services.validate.edit_commit_schema import tuplize
from services.validate.edit_commit_store import latest_edit_version, load_edit_index

if TYPE_CHECKING:
    from services.artifact_store.store import ArtifactStore


class EditLookupError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CommittedEditPlanView:
    """A served committed edit plan: identity, content hash, and exact bytes."""

    plan_version: int
    episode_id: str
    plan_artifact_id: str
    content_sha256: str
    payload: bytes


def get_committed_edit_plan(
    artifact_store: ArtifactStore, edit_plan_dir: Path, *, version: int | None = None
) -> CommittedEditPlanView:
    """Serve a committed version (latest when version is None), bytes verified."""

    index = load_edit_index(edit_plan_dir)
    target = latest_edit_version(index) if version is None else version
    if target < 1:
        raise EditLookupError(
            "no_committed_version", "no edit plan version has been committed yet"
        )
    entry = index.versions.get(str(target))
    if entry is None:
        raise EditLookupError(
            "unknown_version", f"version v{target} is not in the committed versions index"
        )
    payload, _ref = artifact_store.reopen(entry.plan_sha256)
    if hashlib.sha256(payload).hexdigest() != entry.plan_sha256:
        raise EditLookupError(
            "content_hash_mismatch", f"version v{target} bytes do not match the index hash"
        )
    return CommittedEditPlanView(
        plan_version=target,
        episode_id=index.episode_id,
        plan_artifact_id=entry.plan_artifact_id,
        content_sha256=entry.plan_sha256,
        payload=payload,
    )


def get_committed_edit_plan_by_artifact_id(
    artifact_store: ArtifactStore, artifact_id: str
) -> CommittedEditPlanView:
    """Serve a committed edit plan by store artifact id; proposals refused."""

    raw = read_file_nofollow(meta_path(artifact_store.store_root, artifact_id))
    if raw is None:
        raise EditLookupError(
            "unknown_artifact", f"artifact {artifact_id} is not in the artifact store"
        )
    try:
        intent = parse_meta_bytes(raw, expected_id=artifact_id)
    except StoreViewError as error:
        raise EditLookupError(error.code, error.detail) from error
    artifact_type = intent.envelope.artifact_type
    if artifact_type == EDIT_PROPOSAL_ARTIFACT_TYPE:
        raise EditLookupError(
            "committed_only",
            f"artifact {artifact_id} is an edit plan PROPOSAL; downstream reads"
            " accept committed edit plan versions only",
        )
    if artifact_type != EDIT_COMMITTED_ARTIFACT_TYPE:
        raise EditLookupError(
            "not_an_edit_plan",
            f"artifact {artifact_id} is a {artifact_type}, not a committed edit plan",
        )
    payload, _ref = artifact_store.reopen(intent.envelope.content_hash)
    document: object = json.loads(payload)
    try:
        plan = CommittedEditPlan.model_validate(tuplize(document))
    except ValidationError as error:
        raise EditLookupError(
            "foreign_plan", f"artifact {artifact_id} bytes are not a committed plan: {error}"
        ) from error
    return CommittedEditPlanView(
        plan_version=int(plan.plan_version[1:]),
        episode_id=plan.episode_id,
        plan_artifact_id=artifact_id,
        content_sha256=intent.envelope.content_hash,
        payload=payload,
    )


def _looks_like_proposal(document: object) -> bool:
    return isinstance(document, dict) and (
        document.get("schema_version") == "edit-plan-v1"
        or ("proposal_id" in document and "plan_version" not in document)
    )


def load_committed_edit_plan_file(path: Path) -> CommittedEditPlan:
    """Refuse proposal FILES: a path is never an acceptable downstream source."""

    try:
        document: object = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise EditLookupError("unreadable_plan_file", str(error)) from error
    if _looks_like_proposal(document):
        raise EditLookupError(
            "committed_only",
            f"{path.name} is an edit plan PROPOSAL file; proposals are never"
            " downstream inputs",
        )
    try:
        return CommittedEditPlan.model_validate(tuplize(document))
    except ValidationError as error:
        raise EditLookupError(
            "not_a_committed_plan", f"{path.name} is not a committed edit plan: {error}"
        ) from error


__all__ = [
    "CommittedEditPlanView",
    "EditLookupError",
    "get_committed_edit_plan",
    "get_committed_edit_plan_by_artifact_id",
    "load_committed_edit_plan_file",
]
