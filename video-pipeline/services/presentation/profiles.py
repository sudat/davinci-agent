"""Presentation profile resolution (narrowing-only, Todo-12 style).

Resolution mirrors :mod:`services.config.resolver`: scalar presentation keys
follow last-wins precedence from System to ApprovedOverride, while the asset
catalog is narrowing-only — a higher layer may drop catalog entries but never
add unregistered ones, and every final binding must exist in the registry
with a matching usage kind. The resolved profile is sealed over canonical
bytes; Job-start snapshot gating lives in :mod:`services.presentation.snapshot`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.contracts.serialization import GENESIS_SHA256
from services.presentation.models import (
    ApprovedPresentationOverride,
    AssetBindings,
    AssetCatalog,
    AudioBrandConfig,
    ChannelPresentationProfile,
    ColorProfileConfig,
    EpisodePresentationProfile,
    GenrePresentationProfile,
    PlacementConfig,
    ResolvedPresentationProfile,
    SubtitleStyleConfig,
    SystemPresentationProfile,
)

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier
    from services.presentation.asset_registry import RegistrySnapshot

type PresentationLayer = (
    GenrePresentationProfile
    | ChannelPresentationProfile
    | EpisodePresentationProfile
    | ApprovedPresentationOverride
)


class ProfileResolutionError(ValueError):
    """A layer widened permissions or referenced an unregistered asset."""


def _narrow_catalog(
    floor: AssetCatalog, layer_id: str, candidates: AssetCatalog
) -> AssetCatalog:
    extra = sorted(set(candidates) - set(floor))
    if extra:
        raise ProfileResolutionError(
            f"layer '{layer_id}' may only narrow the asset catalog: "
            f"{', '.join(extra)} are not granted below"
        )
    return tuple(sorted(set(candidates)))


def _require_registered_bindings(
    bindings: AssetBindings, registry: RegistrySnapshot
) -> None:
    for binding in bindings:
        entry = registry.entry_for(binding.asset_id)
        if entry is None:
            raise ProfileResolutionError(
                f"unregistered asset binding: {binding.asset_id}"
            )
        if entry.usage != binding.kind:
            raise ProfileResolutionError(
                f"asset {binding.asset_id} is registered for {entry.usage}, "
                f"not {binding.kind}"
            )


def _applied_layers(
    episode: EpisodePresentationProfile,
    genre: GenrePresentationProfile | None,
    channel: ChannelPresentationProfile | None,
    override: ApprovedPresentationOverride | None,
) -> list[tuple[Identifier, PresentationLayer]]:
    applied: list[tuple[Identifier, PresentationLayer]] = []
    if genre is not None:
        applied.append((genre.genre_id, genre))
    if channel is not None:
        applied.append((channel.channel_id, channel))
    applied.append((episode.episode_id, episode))
    if override is not None:
        applied.append((override.override_id, override))
    return applied


def resolve_presentation_profile(  # noqa: PLR0913 (layered resolution mirrors services.config.resolver)
    system: SystemPresentationProfile,
    *,
    episode: EpisodePresentationProfile,
    registry: RegistrySnapshot,
    genre: GenrePresentationProfile | None = None,
    channel: ChannelPresentationProfile | None = None,
    override: ApprovedPresentationOverride | None = None,
) -> ResolvedPresentationProfile:
    """Resolve the layered presentation profile into an immutable snapshot."""

    applied = _applied_layers(episode, genre, channel, override)

    catalog: AssetCatalog = system.asset_catalog
    style: SubtitleStyleConfig = system.subtitle_style
    color: ColorProfileConfig = system.color_profile
    audio: AudioBrandConfig = system.audio
    placement: PlacementConfig = system.placement
    bindings: AssetBindings = system.asset_bindings

    for layer_id, layer in applied:
        if layer.asset_catalog is not None:
            catalog = _narrow_catalog(catalog, str(layer_id), layer.asset_catalog)
        if layer.asset_bindings is not None:
            unregistered = sorted(
                binding.asset_id
                for binding in layer.asset_bindings
                if binding.asset_id not in catalog
            )
            if unregistered:
                raise ProfileResolutionError(
                    f"layer '{layer_id}' may not introduce unregistered assets: "
                    f"{', '.join(unregistered)}"
                )
            bindings = layer.asset_bindings
        style = layer.subtitle_style or style
        color = layer.color_profile or color
        audio = layer.audio or audio
        placement = layer.placement or placement

    _require_registered_bindings(bindings, registry)

    draft = ResolvedPresentationProfile(
        episode_id=episode.episode_id,
        layers_applied=tuple(layer_id for layer_id, _ in applied),
        asset_catalog=catalog,
        subtitle_style=style,
        color_profile=color,
        audio=audio,
        placement=placement,
        asset_bindings=bindings,
        profile_snapshot_sha256=GENESIS_SHA256,
    )
    return draft.model_copy(update={"profile_snapshot_sha256": draft.content_hash()})


__all__ = [
    "PresentationLayer",
    "ProfileResolutionError",
    "resolve_presentation_profile",
]
