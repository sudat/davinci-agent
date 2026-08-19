"""Append-only FreezeStore: versioned packages, frozen state, Builder refusal.

Publishing a Freeze Package freezes the job: ``automation_frozen`` is set
irreversibly (a state row claiming ``false`` is tampering and fails
closed), package files are append-only per version, a byte-identical
republish is idempotent, and a re-freeze of a frozen job appends a NEW
version instead of overwriting. Any later Builder mutation of a frozen
job is a typed ``FrozenJobRefusal`` raised BEFORE the builder runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import ValidationError

from services.contracts.primitives import PositiveInteger, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.manual_finalization.freeze_models import (  # noqa: TC001 (pydantic resolves it at runtime)
    FreezePackage,
)

T = TypeVar("T")
_VERSION_PATTERN = re.compile(r"^freeze-v(\d+)\.json$")


class FreezeStateError(Exception):
    """The frozen-state file or package files failed integrity validation."""


class FrozenJobRefusal(Exception):  # noqa: N818 (typed-refusal vocabulary, not an error kind)
    """A frozen job can never be built or rebuilt by automation."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"job-frozen: {detail}")
        self.code = "job-frozen"
        self.detail = detail


class FrozenState(StrictModel):
    schema_version: Literal["frozen-state-v1"] = "frozen-state-v1"
    automation_frozen: bool
    version: PositiveInteger
    package_sha256: Sha256
    target_set_hash: Sha256


class PublishedFreeze(StrictModel):
    version: PositiveInteger
    package: FreezePackage
    idempotent: bool = False


class FreezeStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._packages = root / "packages"
        self._state_path = root / "frozen.json"

    def is_frozen(self) -> bool:
        return self.frozen_state() is not None

    def frozen_state(self) -> FrozenState | None:
        if not self._state_path.exists():
            return None
        try:
            state = FrozenState.model_validate_json(self._state_path.read_bytes())
        except ValidationError as error:
            raise FreezeStateError(
                f"frozen-state-invalid (tampered or malformed): {error}"
            ) from error
        if not state.automation_frozen:
            raise FreezeStateError(
                "frozen-state-invalid: automation_frozen is irreversible; "
                "a false value is tampering"
            )
        package_path = self._packages / f"freeze-v{state.version}.json"
        if not package_path.is_file():
            raise FreezeStateError(f"freeze package v{state.version} is missing")
        published = self._read_published(package_path)
        if published.package.package_sha256 != state.package_sha256:
            raise FreezeStateError("frozen-state-invalid: package hash drift")
        return state

    def publish(self, package: FreezePackage) -> PublishedFreeze:
        self._packages.mkdir(parents=True, exist_ok=True)
        versions = self._published_versions()
        if versions:
            latest = self._read_published(
                self._packages / f"freeze-v{versions[-1]}.json"
            )
            if latest.package.package_sha256 == package.package_sha256:
                return PublishedFreeze(
                    version=latest.version, package=latest.package, idempotent=True
                )
        version = (versions[-1] + 1) if versions else 1
        record = PublishedFreeze(version=version, package=package)
        path = self._packages / f"freeze-v{version}.json"
        if path.exists():
            raise FreezeStateError(
                f"freeze-version-conflict: {path.name} already exists; "
                "freeze versions are append-only"
            )
        atomic_write(path, canonical_model_bytes(record))
        state = FrozenState(
            automation_frozen=True,
            version=version,
            package_sha256=package.package_sha256,
            target_set_hash=package.target_set_hash,
        )
        atomic_write(self._state_path, canonical_model_bytes(state))
        return record

    def assert_buildable(self) -> None:
        state = self.frozen_state()
        if state is not None:
            raise FrozenJobRefusal(
                f"the job is frozen by freeze package v{state.version} "
                f"({state.package_sha256}); the Builder refuses any build or "
                "rebuild of a frozen job (PRD 7.4)"
            )

    def guard_build(self, build: Callable[[], T]) -> T:
        self.assert_buildable()
        return build()

    def _published_versions(self) -> tuple[int, ...]:
        if not self._packages.exists():
            return ()
        versions = [
            int(match.group(1))
            for name in (child.name for child in self._packages.iterdir())
            if (match := _VERSION_PATTERN.match(name))
        ]
        return tuple(sorted(versions))

    def _read_published(self, path: Path) -> PublishedFreeze:
        try:
            return PublishedFreeze.model_validate_json(path.read_bytes())
        except ValidationError as error:
            raise FreezeStateError(f"freeze-package-invalid ({path.name}): {error}") from error


__all__ = [
    "FreezeStateError",
    "FreezeStore",
    "FrozenJobRefusal",
    "FrozenState",
    "PublishedFreeze",
]
