"""Typed normalization of MCP tool responses (task 9).

Every normalizer parses one raw tool payload into a frozen ``StrictModel``
with ``extra="forbid"``: unknown keys and missing required keys raise
:class:`NormalizationError` — nothing is silently dropped.  The models here
are the contract surface the probe matrix needs (server info, ``tools/list``,
``resolve_control get_version``, the ``media_analysis`` standard pass, and
deep-shot analysis); task 11 replaces the synthetic seed fixtures with
recorded live payloads and re-runs the contract tests.

The normalizers accept either a decoded JSON object or the raw JSON text
returned by :class:`services.mcp_client.client.McpToolResult`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, Field, ValidationError

from services.contracts.primitives import StrictModel, to_tuple
from services.mcp_client.errors import McpClientError


class NormalizationError(McpClientError):
    """A tool payload could not be normalized into its typed model."""

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"normalization failed for {tool!r}: {reason}")
        self.tool = tool
        self.reason = reason


def _to_float(value: object) -> object:
    """Coerce an integer timestamp to float (strict mode rejects int for float)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _parse_payload(tool: str, payload: object) -> dict[str, object]:
    """Boundary parse: accept a decoded object or raw JSON text.

    The vendor's v2.207.0+ ``dual`` result envelope rides under the reserved
    ``_operation`` key (payload untouched, envelope additive — see the pinned
    ``src/utils/operation_result``). It is transport metadata, not domain
    data, so it is dropped here at the single boundary every normalized
    payload crosses; strict models stay strict about domain keys.
    """
    if isinstance(payload, str):
        try:
            parsed: object = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise NormalizationError(tool, f"payload is not JSON: {exc}") from exc
        payload = parsed
    if not isinstance(payload, Mapping):
        raise NormalizationError(tool, "payload must be a JSON object")
    return {key: value for key, value in dict(payload).items() if key != "_operation"}


def _describe_validation_error(exc: ValidationError) -> str:
    """Flatten a pydantic error list into key-naming, machine-readable text."""
    details: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        error_type = str(error.get("type", ""))
        if error_type == "missing":
            details.append(f"missing required key {loc!r}")
        elif error_type == "extra_forbidden":
            details.append(f"unknown key {loc!r}")
        else:
            details.append(f"invalid value at {loc!r} ({error_type})")
    return "; ".join(details)


def _normalize[T: StrictModel](tool: str, payload: object, model_type: type[T]) -> T:
    """Parse *payload* into *model_type*; any failure becomes NormalizationError."""
    data = _parse_payload(tool, payload)
    try:
        return model_type.model_validate(data)
    except ValidationError as exc:
        raise NormalizationError(tool, _describe_validation_error(exc)) from exc


# ---------------------------------------------------------------------------
# serverInfo (initialize handshake)
# ---------------------------------------------------------------------------


class ServerInfo(StrictModel):
    """The ``serverInfo`` object of an MCP initialize response."""

    name: str
    version: str


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class ToolInfo(StrictModel):
    """One ``tools/list`` entry; ``inputSchema`` is the wire key."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    name: str
    description: str | None = None
    input_schema: dict[str, object] = Field(alias="inputSchema")


class ToolsList(StrictModel):
    """The ``tools/list`` result envelope."""

    tools: Annotated[tuple[ToolInfo, ...], BeforeValidator(to_tuple)]


# ---------------------------------------------------------------------------
# resolve_control {action: get_version} — mirrors the recorded live payload
# ---------------------------------------------------------------------------


class ResolveBuildInfo(StrictModel):
    """The ``build`` block of the live get_version payload."""

    unavailable_on_this_build: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]
    known_gates: int
    note: str


class McpUpdateInfo(StrictModel):
    """The ``mcp.update`` block (update-check state)."""

    status: str
    current_version: str
    update_mode: str
    checked_at: Annotated[float, BeforeValidator(_to_float)]


class McpUpdateDecision(StrictModel):
    """The ``mcp.update_decision`` block."""

    action: str
    reason: str
    update_mode: str


class McpInfo(StrictModel):
    """The ``mcp`` block of the live get_version payload."""

    version: str
    update: McpUpdateInfo
    update_decision: McpUpdateDecision


class ResolveVersionPayload(StrictModel):
    """``resolve_control {action: get_version}`` text payload (live shape)."""

    product: str
    version: Annotated[tuple[int, int, int, int, str], BeforeValidator(to_tuple)]
    version_string: str
    build: ResolveBuildInfo
    mcp: McpInfo


# ---------------------------------------------------------------------------
# media_analysis standard pass (schema v2) and deep-shot analysis
# ---------------------------------------------------------------------------


class ShotBestMoment(StrictModel):
    """A point-in-time editorial highlight inside a shot."""

    time_seconds: float
    why: str


class ShotVisual(StrictModel):
    """Per-shot visual field group (shared by standard and deep passes)."""

    shot_size: str
    framing: str
    camera_motion: str


class ShotEditorial(StrictModel):
    """Per-shot editorial field group (shared by standard and deep passes)."""

    editorial_role: str
    select_potential: str
    best_moment_present: bool
    best_moment: ShotBestMoment | None
    pacing: str
    stillness_type: str | None


class ShotCutPoint(StrictModel):
    """One cut point (``cut_in`` / ``cut_out``) quality assessment."""

    quality: str
    notes: str


class ShotCuttability(StrictModel):
    """Per-shot cuttability field group (shared by standard and deep passes)."""

    cut_in: ShotCutPoint
    cut_out: ShotCutPoint
    match_action_in: bool
    match_action_out: bool


class ShotConfidence(StrictModel):
    """Per-shot confidence ratings (shared by standard and deep passes)."""

    visual: str
    content: str
    audio: str
    editorial: str
    cuttability: str


class ShotContent(StrictModel):
    """Standard-pass content field group."""

    action: str
    location: str


class MediaAnalysisShotDescription(StrictModel):
    """One ``shot_descriptions`` entry of the standard-pass payload."""

    shot_index: int
    time_seconds_start: float
    time_seconds_end: float
    frame_indices_used: Annotated[tuple[int, ...], BeforeValidator(to_tuple)]
    visual: ShotVisual
    content: ShotContent
    editorial: ShotEditorial
    cuttability: ShotCuttability
    confidence: ShotConfidence
    description: str


class MediaAnalysisEditorialClassification(StrictModel):
    """Clip-wide editorial classification of the standard-pass payload."""

    primary_use: str
    select_potential: str
    energy_arc: str
    style: str
    reason: str


class MediaAnalysisStandardReport(StrictModel):
    """The ``media_analysis`` standard-pass (schema v2) payload."""

    success: bool
    provider: str
    schema_version: str
    editorial_classification: MediaAnalysisEditorialClassification
    shot_descriptions: Annotated[
        tuple[MediaAnalysisShotDescription, ...], BeforeValidator(to_tuple)
    ]


class DeepShotPrimarySubject(StrictModel):
    """The primary subject of a deep-shot analysis entry."""

    type: str
    description: str


class DeepShotContent(StrictModel):
    """Deep-shot content field group (adds the primary subject)."""

    primary_subject: DeepShotPrimarySubject
    action: str
    location: str


class DeepShot(StrictModel):
    """One deep-shot analysis entry."""

    shot_index: int
    time_seconds_start: float
    time_seconds_end: float
    frame_indices: Annotated[tuple[int, ...], BeforeValidator(to_tuple)]
    visual: ShotVisual
    content: DeepShotContent
    editorial: ShotEditorial
    cuttability: ShotCuttability
    confidence: ShotConfidence
    description: str


class DeepShotAnalysisReport(StrictModel):
    """The deep-shot analysis payload (``{"shots": [...]}``)."""

    shots: Annotated[tuple[DeepShot, ...], BeforeValidator(to_tuple)]


# ---------------------------------------------------------------------------
# Public normalizers
# ---------------------------------------------------------------------------


def normalize_server_info(payload: object) -> ServerInfo:
    """Normalize the ``serverInfo`` object of an initialize response."""
    return _normalize("serverInfo", payload, ServerInfo)


def normalize_tools_list(payload: object) -> ToolsList:
    """Normalize a ``tools/list`` result envelope."""
    return _normalize("tools/list", payload, ToolsList)


def normalize_resolve_version(payload: object) -> ResolveVersionPayload:
    """Normalize the ``resolve_control get_version`` text payload."""
    return _normalize("resolve_control get_version", payload, ResolveVersionPayload)


def normalize_media_analysis_standard(payload: object) -> MediaAnalysisStandardReport:
    """Normalize the ``media_analysis`` standard-pass (schema v2) payload."""
    return _normalize("media_analysis standard", payload, MediaAnalysisStandardReport)


def normalize_deep_shot_analysis(payload: object) -> DeepShotAnalysisReport:
    """Normalize the deep-shot analysis payload."""
    return _normalize("media_analysis deep", payload, DeepShotAnalysisReport)


__all__ = [
    "DeepShot",
    "DeepShotAnalysisReport",
    "DeepShotContent",
    "DeepShotPrimarySubject",
    "McpInfo",
    "McpUpdateDecision",
    "McpUpdateInfo",
    "MediaAnalysisEditorialClassification",
    "MediaAnalysisShotDescription",
    "MediaAnalysisStandardReport",
    "NormalizationError",
    "ResolveBuildInfo",
    "ResolveVersionPayload",
    "ServerInfo",
    "ShotBestMoment",
    "ShotConfidence",
    "ShotContent",
    "ShotCutPoint",
    "ShotCuttability",
    "ShotEditorial",
    "ShotVisual",
    "ToolInfo",
    "ToolsList",
    "normalize_deep_shot_analysis",
    "normalize_media_analysis_standard",
    "normalize_resolve_version",
    "normalize_server_info",
    "normalize_tools_list",
]
