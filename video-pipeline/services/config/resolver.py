"""Layered resolution: System < Genre < Channel < Episode < ApprovedOverride.

Scalar knobs (retention, budget) follow last-wins precedence. Permission
inventories (cloud allowlist, per-stage data classes, path allowlist roots) are
narrowing-only: a higher layer may drop entries or tighten ``fixture_only`` but
any widening attempt is a resolution error. The result is an immutable
``ResolvedConfig`` snapshot whose ``resolved_config_sha256`` hashes the
canonical bytes, so identical inputs resolve byte-identically and changed
inputs always change the hash.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.config.models import (
    ApprovedOverride,
    ChannelConfig,
    CloudAllowlistEntry,
    EpisodeConfig,
    GenreConfig,
    PathAllowlist,
    ResolvedConfig,
    StageDataClasses,
    SystemConfig,
)

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier

type ConfigLayer = GenreConfig | ChannelConfig | EpisodeConfig | ApprovedOverride


class ResolutionError(ValueError):
    """A layer attempted to widen permissions beyond the layer below."""


def resolve(
    system: SystemConfig,
    *,
    episode: EpisodeConfig,
    genre: GenreConfig | None = None,
    channel: ChannelConfig | None = None,
    override: ApprovedOverride | None = None,
) -> ResolvedConfig:
    """Resolve the layered configuration into an immutable snapshot."""

    applied: list[tuple[Identifier, ConfigLayer]] = []
    if genre is not None:
        applied.append((genre.genre_id, genre))
    if channel is not None:
        applied.append((channel.channel_id, channel))
    applied.append((episode.episode_id, episode))
    if override is not None:
        applied.append((override.override_id, override))

    retention = system.retention
    data_classes = system.data_classes
    cloud_allowlist = system.cloud_allowlist
    roots = system.path_allowlist.roots
    budget = system.budget

    for layer_id, layer in applied:
        if layer.cloud_allowlist is not None:
            cloud_allowlist = _narrow_allowlist(cloud_allowlist, layer_id, layer.cloud_allowlist)
        if layer.data_classes is not None:
            data_classes = _narrow_data_classes(data_classes, layer_id, layer.data_classes)
        if layer.path_allowlist is not None:
            roots = _narrow_roots(roots, layer_id, layer.path_allowlist.roots)
        if layer.retention is not None:
            retention = layer.retention
        if layer.budget is not None:
            budget = layer.budget

    snapshot = ResolvedConfig(
        schema_version="resolved-config-v1",
        episode_id=episode.episode_id,
        layers_applied=tuple(layer_id for layer_id, _ in applied),
        retention=retention,
        data_classes=data_classes,
        cloud_allowlist=cloud_allowlist,
        network=system.network,
        path_allowlist=PathAllowlist(roots=roots),
        budget=budget,
        resolved_config_sha256="0" * 64,
    )
    return snapshot.model_copy(update={"resolved_config_sha256": snapshot.content_hash()})


def _narrow_allowlist(
    floor: tuple[CloudAllowlistEntry, ...],
    layer_id: str,
    entries: tuple[CloudAllowlistEntry, ...],
) -> tuple[CloudAllowlistEntry, ...]:
    granted = {(entry.data_class, entry.stage): entry.fixture_only for entry in floor}
    kept: dict[tuple[str, str], CloudAllowlistEntry] = {}
    for entry in entries:
        key = (entry.data_class, entry.stage)
        if key not in granted:
            raise ResolutionError(
                f"layer '{layer_id}' may only narrow cloud permissions: no grant for "
                f"{entry.data_class} at stage {entry.stage} below (widening refused)"
            )
        if not entry.fixture_only and granted[key]:
            raise ResolutionError(
                f"layer '{layer_id}' may only narrow cloud permissions: cannot loosen "
                f"fixture_only for {entry.data_class} at stage {entry.stage}"
            )
        kept[key] = entry
    return tuple(kept[key] for key in sorted(kept))


def _narrow_data_classes(
    floor: tuple[StageDataClasses, ...],
    layer_id: str,
    entries: tuple[StageDataClasses, ...],
) -> tuple[StageDataClasses, ...]:
    granted = {item.stage: frozenset(item.classes) for item in floor}
    for item in entries:
        if item.stage not in granted:
            raise ResolutionError(
                f"layer '{layer_id}' may only narrow data classes: stage {item.stage} "
                "is not granted below (widening refused)"
            )
        extra = sorted(set(item.classes) - granted[item.stage])
        if extra:
            raise ResolutionError(
                f"layer '{layer_id}' may only narrow data classes: {', '.join(extra)} "
                f"at stage {item.stage} are not granted below (widening refused)"
            )
    return entries


def _narrow_roots(
    floor: tuple[str, ...], layer_id: str, roots: tuple[str, ...]
) -> tuple[str, ...]:
    floor_paths = [Path(root) for root in floor]
    for root in roots:
        candidate = Path(root)
        if not any(candidate == base or candidate.is_relative_to(base) for base in floor_paths):
            raise ResolutionError(
                f"layer '{layer_id}' may only narrow the path allowlist: root {root} is "
                "outside the roots granted below (widening refused)"
            )
    return tuple(sorted(set(roots)))


__all__ = ["ResolutionError", "resolve"]
