"""ChannelProductionKitV1 models — task 36."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.reference_learning.models import PreferenceDomain

EXPECTED_RECIPE_COUNT: int = 12


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _tuple_domains(value: object) -> object:
    if isinstance(value, list):
        out: list[PreferenceDomain] = []
        for item in value:
            if isinstance(item, str):
                out.append(PreferenceDomain(item))
            else:
                out.append(item)  # type: ignore[arg-type]
        return tuple(out)
    return value


# RecipeId: slash-namespaced, e.g. "subtitle/default"
RecipeId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._-]*\/[a-z0-9][a-z0-9._-]*$",
        strict=True,
    ),
]

# Kebab capability id
KebabId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$", strict=True),
]

FallbackKind = Literal["legacy_direct", "template_external", "manual"]


class ParameterBoundV1(StrictModel):
    """Single tunable parameter bounds."""

    min: Annotated[float, Field(strict=False)]
    max: Annotated[float, Field(strict=False)]
    default: Annotated[float, Field(strict=False)]
    unit: Annotated[str, Field(min_length=1, strict=True)] | None = None

    @model_validator(mode="after")
    def check_bounds(self) -> ParameterBoundV1:
        if self.min > self.max:
            raise PydanticCustomError(
                "param_bounds_inverted",
                "param bound min {min} > max {max}",
                {"min": str(self.min), "max": str(self.max)},
            )
        if not (self.min <= self.default <= self.max):
            raise PydanticCustomError(
                "param_default_out_of_range",
                "param default {default} not in [{min}, {max}]",
                {
                    "default": str(self.default),
                    "min": str(self.min),
                    "max": str(self.max),
                },
            )
        return self


class CapabilityBindingV1(StrictModel):
    """Binding to an MCP capability row."""

    capability: KebabId
    accepted_only: bool = False


class ProvenanceV1(StrictModel):
    """License/provenance for external assets."""

    origin: Annotated[str, Field(min_length=1, strict=True)]
    license: Annotated[str, Field(min_length=1, strict=True)]
    assets: Annotated[tuple[str, ...], BeforeValidator(_tuple)] = Field(default=())


class ProductionRecipeV1(StrictModel):
    """One tested production recipe."""

    recipe_id: RecipeId
    semantic_intent: Annotated[str, Field(min_length=1, strict=True)]
    capability_binding: CapabilityBindingV1
    parameter_bounds: dict[str, ParameterBoundV1]
    applicability_conditions: Annotated[str, Field(min_length=1, strict=True)]
    taste_domains: Annotated[
        tuple[PreferenceDomain, ...],
        BeforeValidator(_tuple_domains),
    ]
    preview_fixture: Annotated[str, Field(min_length=1, strict=True)]
    expected_readback: Annotated[str, Field(min_length=1, strict=True)]
    provenance: ProvenanceV1
    fallback: FallbackKind

    @field_validator("parameter_bounds")
    @classmethod
    def check_parameter_bounds_not_empty(
        cls, value: dict[str, ParameterBoundV1]
    ) -> dict[str, ParameterBoundV1]:
        if not value:
            raise PydanticCustomError(
                "parameter_bounds_empty",
                "parameter_bounds must not be empty",
            )
        return value

    @field_validator("taste_domains")
    @classmethod
    def check_taste_domains_unique(
        cls, value: tuple[PreferenceDomain, ...]
    ) -> tuple[PreferenceDomain, ...]:
        if len(set(value)) != len(value):
            raise PydanticCustomError(
                "duplicate_taste_domain",
                "taste_domains must be unique",
            )
        return value


class ChannelProductionKitV1(StrictModel):
    """Versioned set of 12 tested production recipes."""

    kit_version: Annotated[str, Field(min_length=1, strict=True)]
    recipes: Annotated[tuple[ProductionRecipeV1, ...], BeforeValidator(_tuple)]

    @model_validator(mode="after")
    def check_count_and_unique(self) -> ChannelProductionKitV1:
        if len(self.recipes) != EXPECTED_RECIPE_COUNT:
            raise PydanticCustomError(
                "kit_recipe_count",
                "kit must contain exactly 12 recipes, got {count}",
                {"count": str(len(self.recipes))},
            )
        ids = [r.recipe_id for r in self.recipes]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "kit_duplicate_recipe_id",
                "kit recipe_ids must be unique",
            )
        return self


__all__ = [
    "CapabilityBindingV1",
    "ChannelProductionKitV1",
    "ParameterBoundV1",
    "ProductionRecipeV1",
    "ProvenanceV1",
    "RecipeId",
]
