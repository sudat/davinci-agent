"""Presentation Preview v2 (task 60, W6 scope): stub-launch path.

The real low-cost presentation build (lightweight renderer or Resolve/MCP
build over the quality-domain compile) arrives with task 38's compiler and
task 39's runner. Until then this module pins the LAUNCH surface: a stub
``McpExecutionPlan`` is shape-validated against the Timeline IR v2 (episode
binding + canonical IR sha) and produces a placeholder trace — no media is
fabricated. Task 38 replaces the stub model with the real
``McpExecutionPlan``; the launch contract (episode + ir_sha binding) is
expected to carry over unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    ResolveFreeModel,
    Sha256,
    StrictModel,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.errors import PreviewError

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2

STUB_SCHEMA_VERSION: Final = "mcp-execution-plan-stub-v1"
PLACEHOLDER_NOTE: Final = (
    "W6 launch test only: the real low-cost presentation build arrives with "
    "task 39's runner; no media is fabricated at this rung"
)


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class PresentationPreviewError(PreviewError):
    """The stub launch contract was violated (episode/staleness/shape)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class StubStepV2(StrictModel):
    """One planned MCP execution step in the stub plan."""

    step_id: Identifier
    capability: str = Field(min_length=1, strict=True)
    note: str = ""


class McpExecutionPlanStub(ResolveFreeModel):
    """Minimal stand-in for task 38's McpExecutionPlan (launch contract)."""

    schema_version: Literal["mcp-execution-plan-stub-v1"]
    episode_id: Identifier
    ir_sha256: Sha256
    steps: Annotated[tuple[StubStepV2, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)

    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self)).hexdigest()


class PresentationStubBinding(StrictModel):
    """How the launch was bound: which stub, verbatim by hash."""

    stub_sha256: Sha256
    episode_id: Identifier
    step_count: int = Field(gt=0, strict=True)


class PresentationPreviewTraceV2(StrictModel):
    """Placeholder trace for the stub launch (status says so, honestly)."""

    schema_version: Literal["presentation-preview-trace-v2"]
    episode_id: Identifier
    ir_sha256: Sha256
    rate: RationalFrameRate
    status: Literal["placeholder-not-rendered"]
    stub: PresentationStubBinding
    note: str = Field(min_length=1, strict=True)


def render_presentation_preview(
    ir_v2: TimelineIrV2,
    execution_plan_stub: McpExecutionPlanStub,
    *,
    output_path: Path,
) -> PresentationPreviewTraceV2:
    """Validate the stub against the IR and write the placeholder trace."""

    if execution_plan_stub.episode_id != ir_v2.episode_id:
        raise PresentationPreviewError(
            "episode-mismatch",
            f"stub episode {execution_plan_stub.episode_id} does not match the IR episode "
            f"{ir_v2.episode_id}",
        )
    ir_sha = hashlib.sha256(canonical_model_bytes(ir_v2)).hexdigest()
    if execution_plan_stub.ir_sha256 != ir_sha:
        raise PresentationPreviewError(
            "stale-ir-binding",
            f"stub was built against ir sha {execution_plan_stub.ir_sha256} but the IR "
            f"hashes to {ir_sha}",
        )
    trace = PresentationPreviewTraceV2(
        schema_version="presentation-preview-trace-v2",
        episode_id=ir_v2.episode_id,
        ir_sha256=ir_sha,
        rate=ir_v2.rate,
        status="placeholder-not-rendered",
        stub=PresentationStubBinding(
            stub_sha256=execution_plan_stub.canonical_sha256(),
            episode_id=execution_plan_stub.episode_id,
            step_count=len(execution_plan_stub.steps),
        ),
        note=PLACEHOLDER_NOTE,
    )
    atomic_write(output_path, canonical_model_bytes(trace))
    return trace


__all__ = [
    "PLACEHOLDER_NOTE",
    "McpExecutionPlanStub",
    "PresentationPreviewError",
    "PresentationPreviewTraceV2",
    "PresentationStubBinding",
    "StubStepV2",
    "render_presentation_preview",
]
