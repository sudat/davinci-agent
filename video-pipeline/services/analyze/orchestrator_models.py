"""Strict models and deterministic keys for the analyzer orchestrator (Todo 38).

The cache key binds the Edit-Source WORLD hash (every declared media file of
the episode's edit source), the analyzer identity (name + version), the
parameter hash (sha over the frozen orchestration parameters — callers fold
input-artifact hashes such as the transcript content hash into it), and the
model/prompt hash when the analyzer is model-driven. A missing model/prompt
hash and the empty string are the same absence, exactly once, by rule.

Scale discipline: keys and hashes are hex sha256 strings only; no floats.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.analyze.asr_models import TranscriptArtifact
from services.analyze.candidate_models import AnalysisArtifact
from services.analyze.visual_models import VisualAnalysisArtifact
from services.contracts.primitives import (
    ArtifactRef,
    Identifier,
    Sha256,
    SourceId,
    StrictModel,
)
from services.foundation_io import sha256_file

AnalyzerArtifact = TranscriptArtifact | AnalysisArtifact | VisualAnalysisArtifact

FAULT_BEFORE_PUBLISH: Final = "before_publish"
FAULT_BEFORE_INDEX: Final = "before_index"

ParameterScalar = str | int | bool
ParameterValue = ParameterScalar | tuple[ParameterScalar, ...]


class OrchestratorError(Exception):
    """Orchestrator failure with a machine-readable label."""

    def __init__(self, label: str, detail: str) -> None:
        super().__init__(f"{label}: {detail}")
        self.label = label
        self.detail = detail


class OrchestratorCacheConflictError(OrchestratorError):
    """A cache key is bound to foreign/tampered state; adoption refused."""

    def __init__(self, detail: str) -> None:
        super().__init__("orchestrator_cache_conflict", detail)


class EditSourceFile(StrictModel):
    """One declared media file of the episode's edit source."""

    role: Identifier
    path: str
    sha256: Sha256

    @model_validator(mode="after")
    def require_absolute_local_path(self) -> EditSourceFile:
        if "://" in self.path or not Path(self.path).is_absolute():
            raise PydanticCustomError(
                "path_not_local", "edit-source file must be an absolute local path"
            )
        return self


class EditSourceRef(StrictModel):
    """The run's committed edit source: source id plus declared media files."""

    source_id: SourceId
    files: tuple[EditSourceFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_roles(self) -> EditSourceRef:
        roles = [item.role for item in self.files]
        if len(set(roles)) != len(roles):
            raise PydanticCustomError("duplicate_role", "edit-source roles must be unique")
        return self


def spec_parameter_hash(params: Mapping[str, ParameterValue]) -> str:
    """Deterministic sha over frozen orchestration parameters (canonical JSON)."""

    canonical = json.dumps(
        dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def edit_source_world_sha(edit_source: EditSourceRef) -> str:
    """World hash over the source id and every declared file hash."""

    payload = {
        "source_id": edit_source.source_id,
        "files": sorted(
            (item.role, item.sha256) for item in edit_source.files
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_edit_source(edit_source: EditSourceRef) -> str:
    """Re-hash every declared file; return the world sha or refuse on drift."""

    for item in edit_source.files:
        try:
            actual = sha256_file(Path(item.path))
        except OSError as error:
            raise OrchestratorError(
                "edit-source-unreadable", f"{item.role} at {item.path}: {error}"
            ) from error
        if actual != item.sha256:
            raise OrchestratorError(
                "edit-source-drift",
                f"{item.role} at {item.path} hashes {actual}, declared {item.sha256}",
            )
    return edit_source_world_sha(edit_source)


def orchestration_cache_key(*, edit_source_sha256: str, spec: AnalyzerSpec) -> str:
    """Deterministic sha256 over world sha + analyzer identity + parameters."""

    material = "\n".join(
        (
            edit_source_sha256,
            spec.analyzer_name,
            spec.analyzer_version,
            spec.parameter_hash,
            spec.model_prompt_hash or "",
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


class AnalyzerSpec(StrictModel):
    """One analyzer invocation identity: name, version, parameters, prompt."""

    analyzer_name: Identifier
    analyzer_version: Identifier
    parameter_hash: Sha256
    model_prompt_hash: Sha256 | None = None


class OrchestrationResult(StrictModel):
    """Per-spec outcome: cache status, published artifact ref, staleness."""

    spec: AnalyzerSpec
    cache_status: Literal["hit", "miss"]
    artifact: ArtifactRef | None
    superseded: bool = False
    publish_idempotent: bool = False


class AnalyzerFailure(StrictModel):
    """Structured, isolated per-analyzer failure (never raises past the run)."""

    spec: AnalyzerSpec
    error_label: str = Field(min_length=1)
    detail: str


class OrchestrationRecord(StrictModel):
    """One full orchestration run: results, failures, supersession, index."""

    edit_source_sha256: str
    results: tuple[OrchestrationResult, ...]
    failures: tuple[AnalyzerFailure, ...]
    superseded_artifacts: tuple[ArtifactRef, ...]
    index_path: str


FaultHook = Callable[[str], None]


__all__ = [
    "FAULT_BEFORE_INDEX",
    "FAULT_BEFORE_PUBLISH",
    "AnalyzerArtifact",
    "AnalyzerFailure",
    "AnalyzerSpec",
    "EditSourceFile",
    "EditSourceRef",
    "FaultHook",
    "OrchestrationRecord",
    "OrchestrationResult",
    "OrchestratorCacheConflictError",
    "OrchestratorError",
    "edit_source_world_sha",
    "orchestration_cache_key",
    "spec_parameter_hash",
    "verify_edit_source",
]
