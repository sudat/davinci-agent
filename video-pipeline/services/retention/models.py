"""Strict models for conservative retention and safe garbage collection.

Managed-tree contract (PRD 28 + Todo 63): the GC manages ONLY real
directories directly under the jobs root; each job directory carries
``job-state.json`` (investigation-hold state) and may carry a sealed
``retention-registry.json`` mapping job-relative paths to content
hashes. Reconciliation is deletion-gating: a rebuildable file may be
deleted only when the registry vouches for exactly the bytes on disk.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

POLICY_SCHEMA = "retention-policy-v1"
JOB_STATE_SCHEMA = "retention-job-state-v1"
REGISTRY_SCHEMA = "retention-registry-v1"

JOB_STATE_FILE = "job-state.json"
REGISTRY_FILE = "retention-registry.json"

ManagedName = Annotated[str, Field(min_length=1, strict=True)]
RelativePath = Annotated[str, Field(min_length=1, strict=True)]

JobHoldStatus = Literal["ACTIVE", "NEEDS_HUMAN", "FAILED", "FROZEN"]

_NAME_FIELDS = (
    "authoritative_names",
    "manual_finalization_names",
    "rebuildable_names",
    "runtime_cache_names",
)


def _canonical_name_lists(value: object) -> object:
    """Normalize JSON lists to tuples and validate every managed name."""

    if not isinstance(value, dict):
        return value
    for field_name in _NAME_FIELDS:
        names = value.get(field_name)
        if not isinstance(names, list | tuple):
            continue
        seen: set[str] = set()
        checked: list[str] = []
        for name in names:
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
            ):
                raise PydanticCustomError(
                    "name_shape",
                    "managed names must be single path segments without separators: {name}",
                    {"name": name},
                )
            if name in seen:
                raise PydanticCustomError(
                    "duplicate_name",
                    "managed name declared twice: {name}",
                    {"name": name},
                )
            seen.add(name)
            checked.append(name)
        value = {**value, field_name: tuple(checked)}
    return value


class RetentionPolicy(StrictModel):
    """Deletion policy: what is sweepable, and after how long a verified freeze."""

    schema_version: Literal["retention-policy-v1"] = "retention-policy-v1"
    rebuildable_retention_days: int = Field(ge=1, strict=True)
    authoritative_names: tuple[ManagedName, ...] = ()
    manual_finalization_names: tuple[ManagedName, ...] = ()
    rebuildable_names: tuple[ManagedName, ...] = ()
    runtime_cache_names: tuple[ManagedName, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def canonical_name_lists(cls, value: object) -> object:
        return _canonical_name_lists(value)

    @model_validator(mode="after")
    def require_disjoint_classes(self) -> RetentionPolicy:
        classes = (
            self.authoritative_names,
            self.manual_finalization_names,
            self.rebuildable_names,
            self.runtime_cache_names,
        )
        owned: dict[str, int] = {}
        for index, names in enumerate(classes):
            for name in names:
                if name in owned and owned[name] != index:
                    raise PydanticCustomError(
                        "class_overlap",
                        "managed name {name} belongs to more than one retention class",
                        {"name": name},
                    )
                owned[name] = index
        return self

    def sweepable_names(self) -> tuple[str, ...]:
        return (*self.rebuildable_names, *self.runtime_cache_names)


class JobRetentionState(StrictModel):
    """Investigation-hold state for one job; FROZEN requires a freeze time."""

    schema_version: Literal["retention-job-state-v1"] = "retention-job-state-v1"
    job_id: Identifier
    status: JobHoldStatus
    frozen_at_epoch_s: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def require_freeze_time_iff_frozen(self) -> JobRetentionState:
        if self.status == "FROZEN" and self.frozen_at_epoch_s is None:
            raise PydanticCustomError(
                "frozen_at", "FROZEN jobs must declare frozen_at_epoch_s"
            )
        if self.status != "FROZEN" and self.frozen_at_epoch_s is not None:
            raise PydanticCustomError(
                "frozen_at", "only FROZEN jobs may declare frozen_at_epoch_s"
            )
        return self


class RegisteredPath(StrictModel):
    sha256: Sha256


class _RegistryBody(StrictModel):
    schema_version: Literal["retention-registry-v1"] = "retention-registry-v1"
    entries: dict[RelativePath, RegisteredPath] = Field(default_factory=dict)


def _reject_bad_registry_path(path: str) -> None:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise PydanticCustomError(
            "path_shape",
            "registry keys must be relative POSIX paths without traversal: {path}",
            {"path": path},
        )


class RetentionRegistry(StrictModel):
    """Sealed path→hash registry; any edit without resealing is rejected."""

    schema_version: Literal["retention-registry-v1"] = "retention-registry-v1"
    entries: dict[RelativePath, RegisteredPath] = Field(default_factory=dict)
    registry_sha256: Sha256

    @model_validator(mode="after")
    def require_sealed(self) -> RetentionRegistry:
        for path in self.entries:
            _reject_bad_registry_path(path)
        body = _RegistryBody(schema_version=self.schema_version, entries=self.entries)
        seal = hashlib.sha256(canonical_model_bytes(body)).hexdigest()
        if seal != self.registry_sha256:
            raise PydanticCustomError(
                "registry_seal",
                "registry seal mismatch: retention registry bytes are not trustworthy",
            )
        return self

    @classmethod
    def mint(cls, entries: dict[str, RegisteredPath]) -> RetentionRegistry:
        body = _RegistryBody(schema_version=REGISTRY_SCHEMA, entries=dict(entries))
        return cls(
            schema_version=REGISTRY_SCHEMA,
            entries=dict(entries),
            registry_sha256=hashlib.sha256(canonical_model_bytes(body)).hexdigest(),
        )


__all__ = [
    "JOB_STATE_FILE",
    "JOB_STATE_SCHEMA",
    "POLICY_SCHEMA",
    "REGISTRY_FILE",
    "REGISTRY_SCHEMA",
    "JobHoldStatus",
    "JobRetentionState",
    "RegisteredPath",
    "RetentionPolicy",
    "RetentionRegistry",
]
