"""Strict layered-configuration models (PRD 25: System/Genre/Channel/Episode/Override).

Every model is strict, frozen, and extra-forbidding. Cloud and data permissions
are carried as explicit inventories so the resolver can enforce narrowing-only
inheritance; nothing here is a generic policy platform — only the keys this
pipeline actually resolves.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256
from services.foundation_io import canonical_model_bytes

DataClass = Literal[
    "transcript",
    "ocr_text",
    "review_instruction_text",
    "audio",
    "sampled_frame",
    "original_video",
]


class RetentionPolicy(StrictModel):
    authoritative: Literal["permanent"]
    rebuildable_days: int = Field(ge=1, strict=True)


class StageDataClasses(StrictModel):
    stage: Identifier
    classes: tuple[DataClass, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def canonical_classes(cls, value: object) -> object:
        if isinstance(value, dict):
            classes = value.get("classes")
            if isinstance(classes, list | tuple):
                value = {**value, "classes": tuple(sorted(set(classes)))}
        return value


class CloudAllowlistEntry(StrictModel):
    data_class: DataClass
    stage: Identifier
    fixture_only: bool


class NetworkPosture(StrictModel):
    builder: Literal["loopback"]
    builder_endpoint: str = Field(min_length=1)


def _canonical_allowlist(
    entries: tuple[CloudAllowlistEntry, ...],
) -> tuple[CloudAllowlistEntry, ...]:
    indexed: dict[tuple[str, str], CloudAllowlistEntry] = {}
    for entry in entries:
        key = (entry.data_class, entry.stage)
        if key in indexed:
            raise PydanticCustomError(
                "duplicate_allowlist_entry",
                "duplicate cloud allowlist entry for {data_class}@{stage}",
                {"data_class": entry.data_class, "stage": entry.stage},
            )
        indexed[key] = entry
    return tuple(indexed[key] for key in sorted(indexed))


def _canonical_stage_list(
    stages: tuple[StageDataClasses, ...],
) -> tuple[StageDataClasses, ...]:
    indexed: dict[str, StageDataClasses] = {}
    for item in stages:
        if item.stage in indexed:
            raise PydanticCustomError(
                "duplicate_stage",
                "duplicate data-class declaration for stage {stage}",
                {"stage": item.stage},
            )
        indexed[item.stage] = item
    return tuple(indexed[stage] for stage in sorted(indexed))


def _canonical_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    for root in roots:
        if not root.startswith("/"):
            raise PydanticCustomError(
                "root_not_absolute",
                "path allowlist roots must be absolute: {root}",
                {"root": root},
            )
    return tuple(sorted(set(roots)))


type CloudAllowlistEntries = Annotated[
    tuple[CloudAllowlistEntry, ...], AfterValidator(_canonical_allowlist)
]
type StageDataClassesList = Annotated[
    tuple[StageDataClasses, ...], AfterValidator(_canonical_stage_list)
]
type AllowlistRoots = Annotated[tuple[str, ...], AfterValidator(_canonical_roots)]


class PathAllowlist(StrictModel):
    roots: AllowlistRoots = Field(min_length=1)


class BudgetPolicy(StrictModel):
    """Retry/cost knobs; the no-retry classes are pinned to the Todo-11 table."""

    transient_max_attempts: int = Field(ge=1, strict=True)
    permanent_max_attempts: Literal[1] = 1
    blocking_human_max_attempts: Literal[1] = 1
    max_stage_cost_units: int = Field(ge=0, strict=True)
    max_job_cost_units: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_job_ceiling_covers_stage(self) -> BudgetPolicy:
        if self.max_job_cost_units < self.max_stage_cost_units:
            raise PydanticCustomError(
                "job_ceiling",
                "max_job_cost_units must cover max_stage_cost_units",
            )
        return self


class SystemConfig(StrictModel):
    schema_version: Literal["system-config-v1"]
    retention: RetentionPolicy
    data_classes: StageDataClassesList
    cloud_allowlist: CloudAllowlistEntries
    network: NetworkPosture
    path_allowlist: PathAllowlist
    budget: BudgetPolicy


class _LayerKeys(StrictModel):
    """Shared optional keys; higher layers may only narrow the inventories."""

    retention: RetentionPolicy | None = None
    data_classes: StageDataClassesList | None = None
    cloud_allowlist: CloudAllowlistEntries | None = None
    path_allowlist: PathAllowlist | None = None
    budget: BudgetPolicy | None = None


class GenreConfig(_LayerKeys):
    genre_id: Identifier


class ChannelConfig(_LayerKeys):
    channel_id: Identifier


class EpisodeConfig(_LayerKeys):
    episode_id: Identifier
    genre: Identifier | None = None
    channel: Identifier | None = None


class ApprovedOverride(_LayerKeys):
    override_id: Identifier
    approved_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ResolvedConfig(StrictModel):
    """Immutable resolved-configuration snapshot (hash over canonical bytes)."""

    schema_version: Literal["resolved-config-v1"]
    episode_id: Identifier
    layers_applied: tuple[Identifier, ...]
    retention: RetentionPolicy
    data_classes: StageDataClassesList
    cloud_allowlist: CloudAllowlistEntries
    network: NetworkPosture
    path_allowlist: PathAllowlist
    budget: BudgetPolicy
    resolved_config_sha256: Sha256

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"resolved_config_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.resolved_config_sha256 == self.content_hash()


__all__ = [
    "ApprovedOverride",
    "BudgetPolicy",
    "ChannelConfig",
    "CloudAllowlistEntry",
    "DataClass",
    "EpisodeConfig",
    "GenreConfig",
    "NetworkPosture",
    "PathAllowlist",
    "ResolvedConfig",
    "RetentionPolicy",
    "StageDataClasses",
    "SystemConfig",
]
