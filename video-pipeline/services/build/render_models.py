"""Strict models and typed failures for deterministic Final Render jobs.

The typed failure mirrors the Todo-11 failure-class rule locally — only
explicitly known-transient codes may retry; every other code (including
unknown codes) is permanent so an unclassified fault can never loop —
because ``services/build`` must not import ``services.job_runner`` (the
Todo-48 builder/state boundary). Records bind a render to its pinned preset
hash and the conformed timeline fingerprint, and a validated output
STRUCTURALLY requires the raw ffprobe JSON plus a clean decode log, so an
API-success-only result cannot be represented.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.models import RenderJobSpec  # noqa: TC001 (pydantic runtime)
from services.resolve_bridge.fixed_presentation_models import (  # noqa: TC001 (pydantic runtime)
    FfprobeStream,
)

TRANSIENT_RENDER_CODES: Final[frozenset[str]] = frozenset(
    {"timeout", "transport_reset", "resolve_disconnect"}
)


class RenderProjectApi(Protocol):
    """The Deliver-page render surface the runner drives (subset of a project)."""

    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool: ...  # noqa: N802 (Resolve API)

    def SetRenderSettings(self, settings: dict[str, object]) -> bool: ...  # noqa: N802 (Resolve API)

    def AddRenderJob(self) -> str: ...  # noqa: N802 (Resolve API)

    def StartRendering(self, job_id: str) -> bool: ...  # noqa: N802 (Resolve API)

    def StopRendering(self) -> None: ...  # noqa: N802 (Resolve API)

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...  # noqa: N802 (Resolve API)

    def GetRenderJobList(self) -> list[dict[str, object]]: ...  # noqa: N802 (Resolve API)


class RenderCancel:
    """Explicit cancel token; the monitor stops the job and fails typed."""

    def __init__(self) -> None:
        self.requested = False
        self.detail = ""


class OutputAllowlist:
    """Declared output roots; a render target outside them is refused."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        if not roots:
            raise ValueError("output allowlist needs at least one declared root")
        self._roots = tuple(root.resolve() for root in roots)

    def permits(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(root in resolved.parents for root in self._roots)

    def refusal_detail(self, path: Path) -> str:
        roots = ", ".join(str(root) for root in self._roots)
        return f"render target {path.resolve()} is outside the declared output allowlist [{roots}]"


class RenderJobFailure(Exception):  # noqa: N818 (typed-refusal vocabulary, not an error kind)
    """A typed render failure; only explicitly transient codes may retry."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        attempts: int = 1,
        attempt_rows: tuple[RenderJobAttempt, ...] = (),
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.attempts = attempts
        self.attempt_rows = attempt_rows

    @property
    def failure_class(self) -> Literal["transient", "permanent"]:
        """The Todo-11 rule, mirrored: unknown codes never retry."""

        return "transient" if self.code in TRANSIENT_RENDER_CODES else "permanent"


class RenderJobRequest(StrictModel):
    """One final-render job: the pinned preset plus the binding fingerprint."""

    preset: RenderJobSpec
    render_dir: str = Field(min_length=1)
    custom_name: str = Field(min_length=1)
    timeline_conformance_fingerprint: Sha256


def preset_sha256(preset: RenderJobSpec) -> Sha256:
    return hashlib.sha256(canonical_model_bytes(preset)).hexdigest()


class RenderJobAttempt(StrictModel):
    attempt: int = Field(ge=1, strict=True)
    job_id: str = Field(min_length=1)
    outcome: Literal[
        "complete", "timeout", "resolve_disconnect", "false_complete", "cancelled"
    ]
    completion_percentage: int = -1
    poll_count: int = Field(ge=0, strict=True)
    detail: str = ""


class RenderJobRecord(StrictModel):
    """The completed job evidence; it always ends in its own completed attempt."""

    preset_sha256: Sha256
    timeline_conformance_fingerprint: Sha256
    output_path: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    attempts: tuple[RenderJobAttempt, ...] = Field(min_length=1)
    poll_count_total: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def require_complete_tail(self) -> RenderJobRecord:
        tail = self.attempts[-1]
        if tail.outcome != "complete" or tail.job_id != self.job_id:
            raise PydanticCustomError(
                "record_not_complete",
                "a render record must end in its own completed attempt",
            )
        return self


class RenderPresetExpectation(StrictModel):
    """The frozen preset expectations every output field is compared against."""

    container_format_name: str = Field(min_length=1)
    video_codec: str = Field(min_length=1)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    r_frame_rate: str = Field(min_length=1)
    pix_fmt: str = Field(min_length=1)
    audio_codec: str = Field(min_length=1)
    audio_sample_rate: int = Field(gt=0, strict=True)
    audio_channels: int = Field(gt=0, strict=True)
    color_space: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    expected_nb_frames: str | None = None


class RenderOutputBinding(StrictModel):
    """The output is bound to one timeline fingerprint, preset, job, and hash."""

    timeline_conformance_fingerprint: Sha256
    preset_sha256: Sha256
    render_job_id: str = Field(min_length=1)
    output_sha256: Sha256


class DecodeEvidence(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)
    exit_code: Literal[0] = 0  # a passing record can only carry a clean decode
    stderr_tail: str


class RenderColorTable(StrictModel):
    color_space: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_range: str | None = None


class RenderMismatch(StrictModel):
    field: str = Field(min_length=1)
    observed: str
    expected: str


class ValidatedRenderOutput(StrictModel):
    """A validated output: raw ffprobe JSON + clean decode are STRUCTURAL."""

    render_job_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    output_size_bytes: int = Field(gt=0, strict=True)
    output_sha256: Sha256
    ffprobe_json: str = Field(min_length=1)
    video: FfprobeStream
    audio: FfprobeStream
    color: RenderColorTable
    decode: DecodeEvidence
    mismatches: tuple[RenderMismatch, ...] = Field(default=(), max_length=0)
    binding: RenderOutputBinding
    passed: Literal[True] = True

    @model_validator(mode="after")
    def require_binding_consistent(self) -> ValidatedRenderOutput:
        if (
            self.binding.render_job_id != self.render_job_id
            or self.binding.output_sha256 != self.output_sha256
        ):
            raise PydanticCustomError(
                "binding_inconsistent",
                "the binding must reference this record's job and output hash",
            )
        return self


__all__ = [
    "TRANSIENT_RENDER_CODES",
    "DecodeEvidence",
    "OutputAllowlist",
    "RenderCancel",
    "RenderColorTable",
    "RenderJobAttempt",
    "RenderJobFailure",
    "RenderJobRecord",
    "RenderJobRequest",
    "RenderMismatch",
    "RenderOutputBinding",
    "RenderPresetExpectation",
    "RenderProjectApi",
    "ValidatedRenderOutput",
    "preset_sha256",
]
