"""Shared path/state context for the CockpitWorkspace mixins."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from services.contracts.primitives import Identifier
from services.episode_cockpit.errors import CockpitNotFoundError
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore

if TYPE_CHECKING:
    from services.job_runner.state_models import JobSnapshot

_IDENTIFIER: TypeAdapter[Identifier] = TypeAdapter(Identifier)


def validated_episode_id(episode_id: str) -> str:
    """Narrow a route path parameter to the Identifier contract or 404."""

    try:
        return _IDENTIFIER.validate_python(episode_id)
    except ValidationError as error:
        raise CockpitNotFoundError(
            "episode-not-found", f"episode id {episode_id!r} is not a valid identifier"
        ) from error


class WorkspaceContext:
    """Dependencies the workspace mixins borrow from CockpitWorkspace.

    Mixins annotate ``_state_store_path`` / ``_episodes_root`` and may call
    ``_episode_dir`` / ``_require_snapshot``; the concrete
    ``CockpitWorkspace`` supplies the paths at construction.
    """

    _state_store_path: Path
    _episodes_root: Path

    def _episode_dir(self, episode_id: str) -> Path:
        return self._episodes_root / episode_id

    def _require_snapshot(self, episode_id: str) -> JobSnapshot:
        validated = validated_episode_id(episode_id)
        try:
            with StateStore.open(self._state_store_path) as store:
                return store.get_job_snapshot(validated)
        except StateStoreError as error:
            if error.code == "job-missing":
                raise CockpitNotFoundError(
                    "episode-not-found",
                    f"no episode {validated} in the job state store",
                ) from error
            raise


__all__ = ["WorkspaceContext", "validated_episode_id"]
