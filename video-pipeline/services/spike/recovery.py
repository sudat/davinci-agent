"""Failed-partial-build injection and fresh-project rebuild proof (recovery).

The injection builds the first linked A/V pair of the fixed 0A request in a
fresh disposable project and then abandons it mid-build (no render, no
report) — the leftover state a crashed build leaves behind. The rebuild then
runs in a brand-new project from the fixed IR; afterwards the partial project
is re-read to prove it was never extended, then deleted as owned cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from services.contracts.build_report import ItemPlacement0A
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
    TrackRef,
)
from services.resolve_bridge.base_cut import _append_pairs, _import_media, _link_pairs, read_back
from services.resolve_bridge.base_cut_plan import ordered_pairs
from services.resolve_bridge.build_report_fingerprint import timeline_fingerprint
from services.resolve_bridge.connection import ResolveConnection
from services.resolve_bridge.fixed_presentation import (
    apply_project_settings,
    ensure_fixed_layout,
    shifted_request,
)
from services.resolve_bridge.fixed_presentation_models import (
    FRAME_ORIGIN,
    TIMELINE_START_TC,
    FixedProjectApi,
    FixedTimelineApi,
)
from services.resolve_bridge.lifecycle import (
    create_disposable_project,
    create_owned_timeline,
    delete_owned_project,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolApi,
        BaseCutRequest,
        BaseCutTimelineApi,
        TimelineSnapshot,
    )
    from services.resolve_bridge.connection import ProjectApi, ProjectManagerApi, ResolveApi


class RecoveryError(Exception):
    """The partial injection or its post-rebuild verification failed."""


def partial_fingerprint(snapshot: TimelineSnapshot, rate: RationalFrameRate) -> str:
    placements = tuple(
        ItemPlacement0A(
            item_id=f"partial-{index:02d}",
            kind=item.kind,
            source=SourceRef(
                source_id="source",
                span=SourceFrameSpan(
                    start_frame=item.source_start, end_frame=item.source_end, rate=rate
                ),
            ),
            record_span=RecordFrameSpan(start_frame=item.record_start, end_frame=item.record_end),
            track=TrackRef(kind=item.kind, index=item.track_index),
            av_link_id=None,
            media_path=item.media_path,
        )
        for index, item in enumerate(snapshot.items)
    )
    return timeline_fingerprint(placements)


def _first_pair_only(request: BaseCutRequest) -> BaseCutRequest:
    pairs = ordered_pairs(request)
    if not pairs:
        raise RecoveryError("request has no linked pairs to inject")
    video, audio = pairs[0]
    return request.model_copy(update={"items": (video, audio)})


def _build_partial(
    manager: ProjectManagerApi,
    manifest: Phase0AFixtureManifest,
    shifted: BaseCutRequest,
    project_name: str,
    timeline_name: str,
) -> TimelineSnapshot:
    project_api = create_disposable_project(manager, project_name)
    apply_project_settings(cast("FixedProjectApi", project_api), manifest)
    timeline = cast("FixedTimelineApi", create_owned_timeline(project_api, timeline_name))
    if not timeline.SetStartTimecode(TIMELINE_START_TC):
        raise RecoveryError(f"SetStartTimecode({TIMELINE_START_TC}) failed")
    ensure_fixed_layout(timeline)
    pool = cast("BaseCutMediaPoolApi", project_api.GetMediaPool())
    media_items = _import_media(pool, shifted)
    handles = _append_pairs(pool, shifted, media_items)
    _link_pairs(cast("BaseCutTimelineApi", timeline), shifted, handles)
    return read_back(cast("BaseCutTimelineApi", timeline))


def inject_partial(
    connection: ResolveConnection,
    manifest: Phase0AFixtureManifest,
    request: BaseCutRequest,
    rate: RationalFrameRate,
    partial_items: int,
) -> tuple[str, str, TimelineSnapshot, str]:
    """Place only the first pair in a fresh project and abandon the build."""

    manager = connection.project_manager()
    project_name = owned_project_name()
    timeline_name = owned_timeline_name()
    shifted = shifted_request(_first_pair_only(request), FRAME_ORIGIN)
    try:
        snapshot = _build_partial(manager, manifest, shifted, project_name, timeline_name)
    except RecoveryError:
        delete_owned_project(manager, project_name)
        raise
    except Exception as error:  # noqa: BLE001 -- cleanup then re-raise as recovery failure
        try:
            delete_owned_project(manager, project_name)
        finally:
            raise RecoveryError(f"partial injection failed: {error}") from error
    if len(snapshot.items) != partial_items:
        delete_owned_project(manager, project_name)
        raise RecoveryError(
            f"partial injection placed {len(snapshot.items)} items, expected {partial_items}"
        )
    return project_name, timeline_name, snapshot, partial_fingerprint(snapshot, rate)


def _find_partial_snapshot(
    project: ProjectApi, timeline_name: str, rate: RationalFrameRate
) -> tuple[int, str]:
    for index in range(1, project.GetTimelineCount() + 1):
        candidate = project.GetTimelineByIndex(index)
        if candidate is not None and candidate.GetName() == timeline_name:
            if not project.SetCurrentTimeline(candidate):
                raise RecoveryError("SetCurrentTimeline failed for partial reread")
            snapshot = read_back(cast("BaseCutTimelineApi", candidate))
            return len(snapshot.items), partial_fingerprint(snapshot, rate)
    raise RecoveryError(f"partial timeline missing: {timeline_name}")


def reread_partial(
    connection: ResolveConnection,
    project_name: str,
    timeline_name: str,
    rate: RationalFrameRate,
) -> tuple[int, str]:
    manager = connection.project_manager()
    current = manager.GetCurrentProject()
    loaded = bool(current is not None and current.GetName() == project_name)
    project = current if loaded else manager.LoadProject(project_name)
    if project is None:
        raise RecoveryError(f"partial project missing from library: {project_name}")
    try:
        return _find_partial_snapshot(project, timeline_name, rate)
    finally:
        if not loaded:
            manager.CloseProject(project)


@dataclass
class RecorderTape:
    """Observed project-manager mutations during a wrapped rebuild run."""

    created: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class RecordingManager:
    """ProjectManager proxy that records Create/Load/Delete project calls."""

    def __init__(self, inner: ProjectManagerApi, tape: RecorderTape) -> None:
        self._inner = inner
        self._tape = tape

    def __getattr__(self, name: str) -> Callable[..., object]:
        return getattr(self._inner, name)

    def CreateProject(self, project_name: str) -> object:
        self._tape.created.append(project_name)
        return self._inner.CreateProject(project_name)

    def LoadProject(self, project_name: str) -> object:
        self._tape.loaded.append(project_name)
        return self._inner.LoadProject(project_name)

    def DeleteProject(self, project_name: str) -> bool:
        self._tape.deleted.append(project_name)
        return self._inner.DeleteProject(project_name)


class RecordingResolve:
    """Resolve proxy exposing a RecordingManager for one rebuild run."""

    def __init__(self, inner: ResolveConnection, tape: RecorderTape) -> None:
        self._inner = inner
        self._manager = RecordingManager(inner.project_manager(), tape)

    def GetProjectManager(self) -> ProjectManagerApi:
        return cast("ProjectManagerApi", self._manager)

    def GetProductName(self) -> str:
        return self._inner.resolve.GetProductName()

    def GetVersion(self) -> list[int | str]:
        return self._inner.resolve.GetVersion()

    def GetVersionString(self) -> str:
        return self._inner.resolve.GetVersionString()


def recording_connection(connection: ResolveConnection) -> tuple[ResolveConnection, RecorderTape]:
    tape = RecorderTape()
    proxy = ResolveConnection(
        resolve=cast("ResolveApi", RecordingResolve(connection, tape)),
        binding=connection.binding,
    )
    return proxy, tape
