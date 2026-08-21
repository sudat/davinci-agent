"""Honesty guards for the live replay: H1 binding, Resolve lease, stale state.

The live replay runs under an exclusive Resolve lease (the
``StateStore.acquire_lease`` pattern), refuses any candidate whose H1
binding does not hash-match the extract identity/manifest, and rejects
superseded or unknown plan-state hashes instead of silently using them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from services.foundation_io import atomic_write, sha256_file
from services.release.errors import ReleaseGateError
from services.release.live_models import H1Evidence
from services.release.models import IDENTITY_NAME, ReleaseIdentity
from services.release.verify import read_manifest

if TYPE_CHECKING:
    from services.job_runner.state_store import StateStore

LEASE_RESOURCE = "resolve-build:f3-live-replay"
LEASE_HOLDER = "f3-final-qa"
LEASE_TTL_SECONDS = 3600


class StaleStateError(Exception):
    """A commit presented a plan sha that is not the current one."""

    def __init__(self, code: Literal["stale_plan_sha", "superseded_plan_sha"], detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class LiveStateGuard:
    """Single-writer commit guard: every artifact binds to the current plan."""

    def __init__(self, plan_sha256: str) -> None:
        self._current = plan_sha256
        self._superseded: frozenset[str] = frozenset()

    @property
    def plan_sha256(self) -> str:
        return self._current

    def rotate(self, plan_sha256: str) -> None:
        """Promote a new plan version; the previous one becomes superseded."""

        if plan_sha256 != self._current:
            self._superseded = self._superseded | {self._current}
            self._current = plan_sha256

    def require(self, plan_sha256: str) -> None:
        if plan_sha256 == self._current:
            return
        if plan_sha256 in self._superseded:
            raise StaleStateError(
                "superseded_plan_sha",
                f"plan {plan_sha256[:12]} is superseded by {self._current[:12]}",
            )
        raise StaleStateError(
            "stale_plan_sha",
            f"plan {plan_sha256[:12]} is not the current plan {self._current[:12]}",
        )

    def commit(
        self, evidence_dir: Path, name: str, payload: bytes, *, plan_sha256: str
    ) -> Path:
        self.require(plan_sha256)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / name
        atomic_write(path, payload)
        return path


class H1BindingError(ReleaseGateError):
    """The supplied H1 binding does not match the candidate extract."""


def verify_h1_binding(extract: Path, binding_path: Path) -> H1Evidence:
    """Validate the binding against the extract and return its evidence.

    The binding must parse, be the extract's own binding (byte-identical),
    bind the extract's git sha, and hash-match its manifest entry.
    """

    from services.release.input_gates import load_h1_binding  # noqa: PLC0415

    if not extract.is_dir():
        raise H1BindingError("missing_release_input", f"candidate extract not found: {extract}")
    manifest, _ = read_manifest(extract)
    binding = load_h1_binding(extract / "inputs")
    staged_binding = extract / "inputs" / "h1" / "binding.json"
    binding_bytes = binding_path.read_bytes() if binding_path.is_file() else b""
    if binding_bytes != staged_binding.read_bytes():
        raise H1BindingError("stale_hash", f"binding is not the extract's own: {binding_path}")
    entry = next((row for row in manifest.entries if row.path == "inputs/h1/binding.json"), None)
    if entry is None or sha256_file(binding_path) != entry.sha256:
        raise H1BindingError("stale_hash", "binding bytes disagree with the manifest entry")
    try:
        identity = ReleaseIdentity.model_validate_json((extract / IDENTITY_NAME).read_bytes())
    except (OSError, ValidationError) as error:
        raise H1BindingError("stale_hash", f"extract identity unreadable: {error}") from error
    if binding.git_sha != identity.git_sha:
        raise H1BindingError(
            "stale_hash",
            f"binding git_sha {binding.git_sha} != extract identity {identity.git_sha}",
        )
    return H1Evidence(
        binding_path=str(binding_path),
        binding_sha256=sha256_file(binding_path),
        episode_id=binding.episode_id,
        git_sha=binding.git_sha,
    )


class ResolveLease:
    """Exclusive Resolve lease over the runtime StateStore lease table."""

    def __init__(
        self,
        db_path: Path,
        *,
        now: Callable[[], int],
        resource: str = LEASE_RESOURCE,
        holder: str = LEASE_HOLDER,
        ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.resource = resource
        self.holder = holder
        self.ttl_seconds = ttl_seconds
        self._now = now
        self._store: StateStore | None = None

    def acquire(self) -> None:
        from services.job_runner.state_store import StateStore  # noqa: PLC0415

        store = StateStore.open(self.db_path)
        store.acquire_lease(
            resource=self.resource,
            holder=self.holder,
            now=self._now(),
            ttl_seconds=self.ttl_seconds,
        )
        self._store = store

    def release(self) -> None:
        if self._store is None:
            return
        self._store.release_lease(resource=self.resource, holder=self.holder, now=self._now() + 1)
        self._store.close()
        self._store = None

    def holders(self) -> list[tuple[str, str]]:
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute("SELECT resource, holder FROM leases").fetchall()
        finally:
            connection.close()
        return [(str(row[0]), str(row[1])) for row in rows]


__all__ = [
    "LEASE_HOLDER",
    "LEASE_RESOURCE",
    "LEASE_TTL_SECONDS",
    "H1BindingError",
    "H1Evidence",
    "LiveStateGuard",
    "ResolveLease",
    "StaleStateError",
    "verify_h1_binding",
]
