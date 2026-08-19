"""Single-writer Clean Builder over disposable Resolve staging timelines.

Public surface: :class:`CleanBuilder` plus the models, protocols, and
typed failures from :mod:`services.build.builder_models`. The builder
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

__all__ = [
    "BuildFailure",
    "BuildInterrupted",
    "BuildLease",
    "BuildOutput",
    "BuildSeams",
    "BuildTools",
    "BuilderWiring",
    "CleanBuilder",
    "ItemReadbackRow",
    "LeaseHeld",
    "NoopSeams",
    "PackageRegistry",
    "PinnedBuildTools",
    "RenderResult",
    "RenderTiming",
    "SubtitleResult",
]
