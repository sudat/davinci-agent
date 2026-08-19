"""Single-writer Clean Builder over disposable Resolve staging timelines.

Public surface: :class:`CleanBuilder` plus the models, protocols, and
typed failures from :mod:`services.build.builder_models`; item-level
Package↔Resolve conformance from :mod:`services.build.conformance`; and
pre-overwrite drift detection from :mod:`services.build.drift`. The builder
promotes only live-verified bridge operations, never mutates human
timelines, never resumes a partial build, and writes no Job State.
"""

from __future__ import annotations

from services.build.builder_models import (
    BuilderWiring,
    BuildFailure,
    BuildInterrupted,
    BuildLease,
    BuildOutput,
    BuildSeams,
    BuildTools,
    ItemReadbackRow,
    LeaseHeld,
    NoopSeams,
    PackageRegistry,
    RenderResult,
    RenderTiming,
    SubtitleResult,
)
from services.build.builder_render import PinnedBuildTools
from services.build.clean_builder import CleanBuilder
from services.build.conformance import ConformanceChecker, verify_built_conformance
from services.build.conformance_capture import ReadbackRow, TimelineReadback, capture_readback
from services.build.conformance_models import ConformanceTable, ItemVerdict
from services.build.drift import DriftDetector, DriftReport, StagingDriftGuard
from services.build.render_jobs import RenderJobRunner
from services.build.render_models import (
    OutputAllowlist,
    RenderCancel,
    RenderJobFailure,
    RenderJobRecord,
    RenderJobRequest,
    RenderPresetExpectation,
    RenderProjectApi,
    ValidatedRenderOutput,
    preset_sha256,
)
from services.build.render_validate import (
    PinnedRenderInspectionTools,
    validate_render_output,
)

__all__ = [
    "BuildFailure",
    "BuildInterrupted",
    "BuildLease",
    "BuildOutput",
    "BuildSeams",
    "BuildTools",
    "BuilderWiring",
    "CleanBuilder",
    "ConformanceChecker",
    "ConformanceTable",
    "DriftDetector",
    "DriftReport",
    "ItemReadbackRow",
    "ItemVerdict",
    "LeaseHeld",
    "NoopSeams",
    "OutputAllowlist",
    "PackageRegistry",
    "PinnedBuildTools",
    "PinnedRenderInspectionTools",
    "ReadbackRow",
    "RenderCancel",
    "RenderJobFailure",
    "RenderJobRecord",
    "RenderJobRequest",
    "RenderJobRunner",
    "RenderPresetExpectation",
    "RenderProjectApi",
    "RenderResult",
    "RenderTiming",
    "StagingDriftGuard",
    "SubtitleResult",
    "TimelineReadback",
    "ValidatedRenderOutput",
    "capture_readback",
    "preset_sha256",
    "validate_render_output",
    "verify_built_conformance",
]
