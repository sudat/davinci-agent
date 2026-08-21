"""Kill seams and retry bounds for the live replay build flow (Todo 67).

``interruptible_connection`` wraps the bridge so the k-th timeline
placement raises the typed :class:`BuildInterrupted` seam (optionally
after running a mid-flight callback such as quitting Resolve), and
:class:`RetryLimiter` bounds how many build attempts may run before the
flow refuses with a typed error instead of looping forever.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from services.resolve_bridge.connection import (
        MediaPoolApi,
        ProjectApi,
        ProjectManagerApi,
        VersionBinding,
    )


class BuildInterrupted(Exception):
    """The declared kill seam fired mid-build (typed, expected)."""

    code = "build_interrupted"


class RetryLimitError(Exception):
    """The bounded attempt budget is exhausted (typed refusal)."""

    code = "retry_limit"


class RetryLimiter:
    """Counting limiter: at most ``max_attempts`` build attempts per build."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be positive, got {max_attempts}")
        self.max_attempts = max_attempts
        self.attempts_used = 0

    def require_can_attempt(self) -> None:
        if self.attempts_used >= self.max_attempts:
            raise RetryLimitError(
                f"retry limit reached: {self.attempts_used}/{self.max_attempts} attempts used"
            )

    def record_attempt(self) -> None:
        self.attempts_used += 1


class _PoolProtocol(Protocol):
    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[object]: ...


class ConnectionLike(Protocol):
    """The connection surface the live flow and seams rely on."""

    def project_manager(self) -> ProjectManagerApi: ...

    @property
    def binding(self) -> VersionBinding: ...


class InterruptiblePool:
    """Delegating media pool that fires the kill seam at the k-th placement."""

    def __init__(
        self,
        pool: MediaPoolApi,
        *,
        interrupt_at: int,
        on_interrupt: Callable[[], None] | None = None,
    ) -> None:
        self._pool = cast("_PoolProtocol", pool)
        self._interrupt_at = interrupt_at
        self._on_interrupt = on_interrupt
        self.placements = 0
        self.interrupted = False

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[object]:
        if not self.interrupted and self.placements >= self._interrupt_at:
            self.interrupted = True
            if self._on_interrupt is not None:
                self._on_interrupt()
            raise BuildInterrupted(
                f"kill seam after {self.placements} placements "
                f"(declared interrupt at {self._interrupt_at})"
            )
        added = list(self._pool.AppendToTimeline(clip_infos))
        self.placements += len(clip_infos)
        return added

    def __getattr__(self, name: str) -> object:
        return getattr(self._pool, name)


class SeamProject:
    """Delegating project whose media pool is the interrupt seam."""

    def __init__(self, project: ProjectApi, pool: InterruptiblePool) -> None:
        self._project = project
        self._pool = pool

    def GetMediaPool(self) -> MediaPoolApi:
        return cast("MediaPoolApi", self._pool)

    def __getattr__(self, name: str) -> object:
        return getattr(self._project, name)


class SeamManager:
    """Delegating project manager that wires the seam into new projects."""

    def __init__(
        self,
        manager: ProjectManagerApi,
        *,
        interrupt_at: int,
        on_interrupt: Callable[[], None] | None,
    ) -> None:
        self._manager = manager
        self._interrupt_at = interrupt_at
        self._on_interrupt = on_interrupt

    def CreateProject(self, project_name: str) -> ProjectApi | None:
        created = self._manager.CreateProject(project_name)
        if created is None:
            return None
        pool = created.GetMediaPool()
        if pool is None:
            return None
        seam = InterruptiblePool(
            pool, interrupt_at=self._interrupt_at, on_interrupt=self._on_interrupt
        )
        return cast("ProjectApi", SeamProject(created, seam))

    def __getattr__(self, name: str) -> object:
        return getattr(self._manager, name)


class SeamConnection:
    """Connection view whose created projects carry the interrupt seam."""

    def __init__(
        self,
        connection: ConnectionLike,
        *,
        interrupt_at: int,
        on_interrupt: Callable[[], None] | None,
    ) -> None:
        self._connection = connection
        self._interrupt_at = interrupt_at
        self._on_interrupt = on_interrupt

    def project_manager(self) -> ProjectManagerApi:
        return cast(
            "ProjectManagerApi",
            SeamManager(
                self._connection.project_manager(),
                interrupt_at=self._interrupt_at,
                on_interrupt=self._on_interrupt,
            ),
        )

    @property
    def binding(self) -> VersionBinding:
        return self._connection.binding

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def interruptible_connection(
    connection: ConnectionLike,
    *,
    interrupt_at: int,
    on_interrupt: Callable[[], None] | None = None,
) -> SeamConnection:
    """Wrap ``connection`` so its next build aborts at the k-th placement."""

    return SeamConnection(connection, interrupt_at=interrupt_at, on_interrupt=on_interrupt)


__all__ = [
    "BuildInterrupted",
    "ConnectionLike",
    "InterruptiblePool",
    "RetryLimitError",
    "RetryLimiter",
    "SeamConnection",
    "interruptible_connection",
]
