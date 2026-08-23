"""Kit preview file ops: manifest listing, candidate files, selection record.

The side_desks.py mixin precedent (task 51): the cockpit owns NO kit state
— ``plan_previews`` (services.production_kit.preview, task 11) writes
``kit-previews/`` and this mixin only reads it back and appends the
operator's runtime choice to ``kit-selections.json``. The manifest and
the record are revalidated from disk on EVERY read (stale-state guard);
a malformed file is a typed 422, never a silently-empty list.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.production_kit.preview import (
    KIT_PREVIEWS_DIR,
    MANIFEST_NAME,
    SELECTIONS_NAME,
    KitDomainSelectionV1,
    KitPreviewError,
    KitPreviewManifestV1,
    KitSelectionRecordV1,
    append_selection_entry,
    latest_selections,
    load_selection_record,
    recipe_selection_from_record,
)
from services.production_kit.registry import load_kit

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.mp4$")


class KitPreviewOps(WorkspaceContext):
    """Reads of kit-previews + appends to the runtime selection record."""

    def list_kit_previews(self, episode_id: str) -> dict[str, object]:
        """Manifest re-derived from disk; absent -> honest not-generated stub."""

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        manifest_path = episode_dir / KIT_PREVIEWS_DIR / MANIFEST_NAME
        if not manifest_path.is_file():
            return {"available": False, "domains": []}
        manifest = _load_manifest(manifest_path)
        selections_path = episode_dir / SELECTIONS_NAME
        latest: dict[str, str | None] = {
            domain.domain: None for domain in manifest.domains
        }
        if selections_path.is_file():
            record = _load_record(selections_path)
            for domain, entry in latest_selections(record).items():
                if domain in latest:
                    latest[domain] = entry.recipe_id
        return {
            "available": True,
            "episode_id": manifest.episode_id,
            "domains": [
                {
                    "domain": domain.domain,
                    "intents": list(domain.intents),
                    "snippet": domain.snippet.model_dump(mode="json"),
                    "candidates": [
                        {
                            "recipe_id": candidate.recipe_id,
                            "semantic_intent": candidate.semantic_intent,
                            "resolved_params": candidate.resolved_params,
                            "parameter_bounds": candidate.parameter_bounds,
                            "file": candidate.file,
                        }
                        for candidate in domain.candidates
                    ],
                    "selection": latest[domain.domain],
                }
                for domain in manifest.domains
            ],
        }

    def kit_preview_file(self, episode_id: str, domain: str, file_name: str) -> Path:
        """Validated candidate mp4 path (regex names; no traversal by construction)."""

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        if not _DOMAIN_RE.fullmatch(domain) or not _FILE_RE.fullmatch(file_name):
            raise CockpitNotFoundError(
                "kit-preview-not-found", f"invalid kit preview path {domain}/{file_name}"
            )
        path = episode_dir / KIT_PREVIEWS_DIR / domain / file_name
        if not path.is_file():
            raise CockpitNotFoundError(
                "kit-preview-not-found", f"no kit preview at {path}"
            )
        return path

    def record_kit_selection(
        self, episode_id: str, domain: str, *, recipe_id: str | None, note: str | None
    ) -> dict[str, object]:
        """Append the operator's A/B/none choice; verify the build round-trip."""

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        manifest = _load_manifest(episode_dir / KIT_PREVIEWS_DIR / MANIFEST_NAME)
        domain_entry = next(
            (entry for entry in manifest.domains if entry.domain == domain), None
        )
        if domain_entry is None:
            raise CockpitUnprocessableError(
                "kit-domain-unknown",
                f"domain {domain!r} is not in the kit preview manifest "
                f"({[entry.domain for entry in manifest.domains]})",
            )
        intent: str | None = None
        if recipe_id is not None:
            candidate = next(
                (c for c in domain_entry.candidates if c.recipe_id == recipe_id), None
            )
            if candidate is None:
                raise CockpitUnprocessableError(
                    "kit-recipe-not-candidate",
                    f"recipe {recipe_id!r} is not a {domain!r} candidate "
                    f"({[c.recipe_id for c in domain_entry.candidates]})",
                )
            intent = candidate.semantic_intent
        entry = KitDomainSelectionV1(
            domain=domain, recipe_id=recipe_id, semantic_intent=intent, note=note,
            recorded_at=_now_iso(),
        )
        record_path = episode_dir / SELECTIONS_NAME
        try:
            record = append_selection_entry(record_path, episode_dir.name, entry)
        except KitPreviewError as error:
            raise CockpitUnprocessableError(error.code, error.detail) from error
        # Write-time round-trip: the recorded choice must resolve to the same
        # RecipeSelection the finishing build would derive for the intent.
        if recipe_id is not None:
            try:
                resolved = recipe_selection_from_record(load_kit(), record, domain)
            except KitPreviewError as error:
                raise CockpitUnprocessableError(error.code, error.detail) from error
            if resolved is None or resolved.recipe.recipe_id != recipe_id:
                raise CockpitUnprocessableError(
                    "kit-selection-roundtrip",
                    f"recorded {recipe_id!r} did not round-trip through select_recipe",
                )
        return {
            "domain": domain,
            "recipe_id": recipe_id,
            "semantic_intent": intent,
            "note": note,
            "recorded_at": entry.recorded_at,
            "entries": len(record.entries),
        }


def _load_manifest(path: Path) -> KitPreviewManifestV1:
    try:
        return KitPreviewManifestV1.model_validate_json(path.read_bytes())
    except OSError as error:
        raise CockpitNotFoundError(
            "kit-previews-not-found", f"no kit preview manifest at {path}"
        ) from error
    except ValueError as error:
        raise CockpitUnprocessableError(
            "kit-manifest-invalid", f"{path} failed strict revalidation: {error}"
        ) from error


def _load_record(path: Path) -> KitSelectionRecordV1:
    try:
        return load_selection_record(path)
    except KitPreviewError as error:
        raise CockpitUnprocessableError(error.code, error.detail) from error


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["KitPreviewOps"]
