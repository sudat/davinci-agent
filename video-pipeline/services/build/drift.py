"""Drift detection before overwrite: block, classify, route to humans.

A timeline the system may rebuild into must first prove it is still exactly
what the package (and, when available, the recorded prior build) says it is.
Any drift — item-level fault, missing/extra item, or a fingerprint that
disagrees with the recorded prior build — is a ``human_mutation`` or shaped
fault, BLOCKS the overwrite, and is published as a structured
:class:`DriftReport` carrying the expected-vs-observed evidence and the three
human routes (Override proposal / new branch / Manual Freeze). Executing
those routes is a human decision and is deliberately out of scope here: the
builder refuses to touch a drifted timeline, full stop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import Field

from services.build.builder_models import BuildFailure
from services.build.builder_session import staging_project_name
from services.build.conformance import ConformanceChecker, capture_readback
from services.build.drift_evidence import DriftItemFault, evidence_lines
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.resolve_bridge.lifecycle import project_names

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from services.build.builder_models import BuilderWiring
    from services.build.conformance_models import ConformanceTable, TableFaultKind
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.base_cut_models import BaseCutTimelineApi
    from services.resolve_bridge.connection import ProjectApi, ProjectManagerApi

DriftRoute = Literal["override_proposal", "new_branch", "manual_freeze"]
DriftKind = Literal[
    "item_media_mismatch",
    "off_by_one",
    "wrong_track",
    "wrong_link",
    "missing_item",
    "extra_item",
    "human_mutation",
]

DRIFT_ROUTES: Final[tuple[DriftRoute, ...]] = (
    "override_proposal",
    "new_branch",
    "manual_freeze",
)
KIND_PRECEDENCE: Final[tuple[TableFaultKind, ...]] = (
    "missing_item",
    "extra_item",
    "item_media_mismatch",
    "wrong_track",
    "wrong_link",
    "off_by_one",
)


class DriftReport(StrictModel):
    timeline_name: str
    package_artifact_id: str
    kind: DriftKind
    expected_fingerprint: Sha256
    observed_fingerprint: Sha256
    recorded_fingerprint: Sha256 | None
    expected: tuple[str, ...]
    observed: tuple[str, ...]
    item_faults: tuple[DriftItemFault, ...]
    routes: tuple[DriftRoute, ...] = Field(default=DRIFT_ROUTES)


class DriftDetector:
    """Compare a conformance table (plus the recorded prior fingerprint)."""

    def check(
        self,
        table: ConformanceTable,
        *,
        recorded_fingerprint: Sha256 | None = None,
    ) -> DriftReport | None:
        conforms = table.all_passed and table.expected_fingerprint == table.observed_fingerprint
        if conforms and (
            recorded_fingerprint is None or recorded_fingerprint == table.observed_fingerprint
        ):
            return None
        if recorded_fingerprint is None or recorded_fingerprint == table.observed_fingerprint:
            kind: DriftKind = _shape_kind(table)
        else:
            # the system recorded a different timeline than the one now
            # observed: someone changed it after our last write
            kind = "human_mutation"
        expected, observed, faults = evidence_lines(table)
        return DriftReport(
            timeline_name=table.timeline_name,
            package_artifact_id=table.package_artifact_id,
            kind=kind,
            expected_fingerprint=table.expected_fingerprint,
            observed_fingerprint=table.observed_fingerprint,
            recorded_fingerprint=recorded_fingerprint,
            expected=expected,
            observed=observed,
            item_faults=faults,
        )


def _shape_kind(table: ConformanceTable) -> DriftKind:
    present: set[str] = set()
    if table.missing_item_ids:
        present.add("missing_item")
    if table.extra_rows:
        present.add("extra_item")
    for verdict in table.items:
        present.update(verdict.faults)
    for kind in KIND_PRECEDENCE:
        if kind in present:
            return cast("DriftKind", kind)
    return "human_mutation"  # fingerprint-only drift: conservative route


class StagingDriftGuard:
    """Pre-overwrite gate over an existing owned staging timeline.

    Loads the staging project this package would rebuild into, captures its
    readback through public APIs, verifies every item against the package,
    and refuses (typed ``drift-blocked`` failure carrying the DriftReport)
    when anything drifted. The drifted timeline is never deleted, mutated,
    or otherwise touched.
    """

    def __init__(
        self,
        checker: ConformanceChecker | None = None,
        detector: DriftDetector | None = None,
    ) -> None:
        self._checker = checker if checker is not None else ConformanceChecker()
        self._detector = detector if detector is not None else DriftDetector()

    def check(
        self,
        manager: ProjectManagerApi,
        package: ResolvePackage,
        sha256_of: Callable[[Path], str],
        *,
        recorded_fingerprint: Sha256 | None = None,
        project_name: str | None = None,
    ) -> DriftReport | None:
        name = (
            staging_project_name(package.content_hash) if project_name is None else project_name
        )
        if name not in project_names(manager):
            return None
        project = manager.LoadProject(name)
        if project is None:
            raise BuildFailure("drift-check-failed", f"LoadProject({name}) returned nothing")
        try:
            timeline = self._sole_timeline(project, name)
            readback = None if timeline is None else capture_readback(timeline, sha256_of)
        finally:
            manager.CloseProject(project)
        if readback is None:
            return None  # a timeline-less husk carries nothing to protect
        table = self._checker.verify(package, readback)
        return self._detector.check(table, recorded_fingerprint=recorded_fingerprint)

    def enforce(
        self,
        manager: ProjectManagerApi,
        package: ResolvePackage,
        wiring: BuilderWiring,
        *,
        project_name: str | None = None,
    ) -> None:
        report = self.check(
            manager,
            package,
            wiring.tools.sha256,
            recorded_fingerprint=wiring.prior_conformance_fingerprint,
            project_name=project_name,
        )
        if report is not None:
            raise BuildFailure("drift-blocked", canonical_model_bytes(report).decode())

    def _sole_timeline(self, project: ProjectApi, name: str) -> BaseCutTimelineApi | None:
        if project.GetTimelineCount() < 1:
            return None
        timeline = project.GetTimelineByIndex(1)
        if timeline is None:
            raise BuildFailure("drift-check-failed", f"{name}: timeline readback failed")
        if not project.SetCurrentTimeline(timeline):
            raise BuildFailure("drift-check-failed", f"{name}: SetCurrentTimeline failed")
        return cast("BaseCutTimelineApi", timeline)


__all__ = [
    "DRIFT_ROUTES",
    "DriftDetector",
    "DriftKind",
    "DriftReport",
    "DriftRoute",
    "StagingDriftGuard",
]
