"""Domain-scoped feature extraction — v43 task 26.

Contract: for each NAMED domain only, produce the PRD 8.5 feature vocabulary
shape. Features for UNNAMED domains are FORBIDDEN (typed rejection). Values
are placeholder ``None``-with-measurement-plan where media measurement does
not yet exist — the CONTRACT is domain-scoping + vocabulary, not measurement.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.reference_learning.models import PreferenceDomain, ReferenceSourceV1


class FeatureDomainNotNamedError(ValueError):
    """Raised when feature extraction is requested for an unnamed domain."""



# ---------------------------------------------------------------------------
# Per-domain feature vocabularies (PRD 8.5, typed optional fields)
# ---------------------------------------------------------------------------


class StoryStructureFeatures(StrictModel):
    """story/speaking structure — PRD 8.5."""

    hook_timing: float | None = Field(default=None, description="measure hook frame offset")
    time_to_thesis: float | None = Field(default=None, description="measure time to thesis")
    setup_evidence_payoff_pattern: str | None = Field(
        default=None, description="pattern tag — needs transcript"
    )
    example_placement: str | None = Field(default=None, description="needs transcript/semantic")
    topic_transition_cadence: float | None = Field(
        default=None, description="measure transition cadence"
    )
    repetition: str | None = Field(default=None, description="detect repetition — placeholder")
    cta_placement: float | None = Field(default=None, description="measure CTA offset")


class PacingFeatures(StrictModel):
    """pacing — PRD 8.5."""

    shot_duration_distributions: str | None = Field(
        default=None, description="measure shot durations"
    )
    preserved_pauses: float | None = Field(default=None, description="measure pauses")
    jump_cut_density: float | None = Field(default=None, description="measure jump-cut density")
    information_density: float | None = Field(default=None, description="needs transcript")
    silence_tolerance: float | None = Field(default=None, description="measure silence tolerance")


class ColorFeatures(StrictModel):
    """color — PRD 8.5."""

    contrast: str | None = Field(default=None, description="measure contrast")
    exposure_tendency: str | None = Field(default=None, description="measure exposure")
    white_balance_tendency: str | None = Field(default=None, description="measure WB")
    saturation: str | None = Field(default=None, description="measure saturation")
    skin_product_handling: str | None = Field(default=None, description="measure skin/product")
    highlight_shadow_behavior: str | None = Field(
        default=None, description="measure highlight/shadow"
    )
    scene_to_scene_consistency: str | None = Field(default=None, description="measure consistency")


class SubtitleFeatures(StrictModel):
    """subtitles — PRD 8.5."""

    characters_per_cue: float | None = Field(default=None, description="measure chars per cue")
    reading_speed: float | None = Field(default=None, description="measure reading speed")
    line_breaks: str | None = Field(default=None, description="measure line breaks")
    position: str | None = Field(default=None, description="measure position")
    emphasis_frequency: float | None = Field(default=None, description="measure emphasis")
    typography_family: str | None = Field(default=None, description="measure typography")
    animation_density: float | None = Field(default=None, description="measure animation")


class BrollFeatures(StrictModel):
    """B-roll — PRD 8.5."""

    frequency: float | None = Field(default=None, description="measure B-roll frequency")
    duration: float | None = Field(default=None, description="measure B-roll duration")
    semantic_distance_from_narration: str | None = Field(
        default=None, description="needs semantic distance"
    )
    lead_lag_timing: float | None = Field(default=None, description="measure lead/lag")
    cutaway_length: float | None = Field(default=None, description="measure cutaway length")


class FramingGraphicsFeatures(StrictModel):
    """framing/graphics — PRD 8.5."""

    punch_in_density: float | None = Field(default=None, description="measure punch-in density")
    crop_magnitude: float | None = Field(default=None, description="measure crop")
    lower_third_title_frequency: float | None = Field(
        default=None, description="measure lower-third frequency"
    )
    transition_density: float | None = Field(default=None, description="measure transitions")


class AudioFeatures(StrictModel):
    """audio — PRD 8.5."""

    dialogue_to_music_relationship: str | None = Field(
        default=None, description="measure dialogue/music relationship"
    )
    ambience_preservation: str | None = Field(default=None, description="measure ambience")
    ducking_behavior: str | None = Field(default=None, description="measure ducking")
    loudness_character: str | None = Field(default=None, description="measure loudness")


# Registry for typed construction
_DOMAIN_FEATURE_TYPES: dict[PreferenceDomain, type[StrictModel]] = {
    PreferenceDomain.story_structure: StoryStructureFeatures,
    PreferenceDomain.pacing: PacingFeatures,
    PreferenceDomain.color: ColorFeatures,
    PreferenceDomain.subtitle: SubtitleFeatures,
    PreferenceDomain.b_roll: BrollFeatures,
    PreferenceDomain.framing_graphics: FramingGraphicsFeatures,
    PreferenceDomain.audio: AudioFeatures,
}


def _validate_scoping(
    named_domains: tuple[PreferenceDomain, ...],
    requested: tuple[PreferenceDomain, ...],
) -> None:
    named_set = set(named_domains)
    for dom in requested:
        if dom not in named_set:
            raise FeatureDomainNotNamedError(
                f"feature domain {dom.value} was not named;"
                f" named={sorted(d.value for d in named_set)}"
            )
    # Also validate via pydantic custom error for uniform handling if needed
    if any(d not in named_set for d in requested):
        raise PydanticCustomError(
            "feature_domain_not_named",
            "feature domain not in named_domains",
        )


def extract_features(  # noqa: C901,PLR0912

    reference_source: ReferenceSourceV1 | Annotated[str, Field(min_length=1)],
    named_domains: tuple[PreferenceDomain, ...] | list[PreferenceDomain],
    *,
    requested_domains: tuple[PreferenceDomain, ...] | list[PreferenceDomain] | None = None,
) -> dict[PreferenceDomain, StrictModel]:
    """Extract domain-scoped placeholder features.

    Args:
        reference_source: validated ReferenceSourceV1 (or source_id string for
            lightweight callers); currently used only for provenance, not
            measurement — values are placeholder None with measurement plans.
        named_domains: domains named by the operator; only these may appear
            in the result. Empty tuple yields empty dict.
        requested_domains: optional explicit request set; if provided it must
            be a subset of ``named_domains`` or a typed rejection is raised.
            This seam enables the cross-domain leak test (requesting an
            unnamed domain). When None, requested == named_domains.

    Returns:
        Dict keyed by domain, each value a typed feature struct with optional
        placeholder fields (all None initially).

    Raises:
        FeatureDomainNotNamedError: if any requested domain is not in named_domains.
        TypeError: if named_domains contains unknown values.
    """
    # Coerce and validate named_domains entries are PreferenceDomain values
    coerced_named: list[PreferenceDomain] = []
    for item in named_domains:
        if isinstance(item, PreferenceDomain):
            coerced_named.append(item)
        elif isinstance(item, str):
            try:
                coerced_named.append(PreferenceDomain(item))
            except ValueError as exc:
                raise FeatureDomainNotNamedError(f"unknown domain value: {item!r}") from exc
        else:
            raise TypeError(
                f"named_domains entries must be PreferenceDomain or str, got {type(item)}"
            )

    if len(set(coerced_named)) != len(coerced_named):
        raise ValueError("named_domains must be unique")

    named_tuple = tuple(coerced_named)

    if requested_domains is None:
        requested_tuple = named_tuple
    else:
        coerced_req: list[PreferenceDomain] = []
        for item in requested_domains:
            if isinstance(item, PreferenceDomain):
                coerced_req.append(item)
            elif isinstance(item, str):
                try:
                    coerced_req.append(PreferenceDomain(item))
                except ValueError as exc:
                    raise FeatureDomainNotNamedError(f"unknown domain value: {item!r}") from exc
            else:
                raise TypeError(
                    f"requested_domains entries must be PreferenceDomain/str, got {type(item)}"
                )
        requested_tuple = tuple(coerced_req)
        _validate_scoping(named_tuple, requested_tuple)

    # Validate that requested is subset of named (defensive double-check)
    _validate_scoping(named_tuple, requested_tuple)

    # Build placeholder features for each requested domain
    out: dict[PreferenceDomain, StrictModel] = {}
    for dom in requested_tuple:
        feat_type = _DOMAIN_FEATURE_TYPES.get(dom)
        if feat_type is None:
            raise FeatureDomainNotNamedError(f"no feature vocabulary for domain {dom.value}")
        out[dom] = feat_type()

    # Final invariant: never return a domain outside named_domains
    for k in out:
        if k not in set(named_tuple):
            raise FeatureDomainNotNamedError(f"leaked domain {k.value} not in named_domains")

    # reference_source is intentionally unused beyond type-checking; keep
    # placeholder to satisfy signature contract and future measurement wiring.
    _ = reference_source
    return out


__all__ = [
    "AudioFeatures",
    "BrollFeatures",
    "ColorFeatures",
    "FeatureDomainNotNamedError",
    "FramingGraphicsFeatures",
    "PacingFeatures",
    "StoryStructureFeatures",
    "SubtitleFeatures",
    "extract_features",
]
