"""MCP analysis import adapter — normalize standard + deep-shot payloads.

Consumes the TYPED models from ``services.mcp_client.response_normalize``
(task 9); never re-implements normalization and never calls the MCP server
(fixtures only). Every shot receives a deterministic shot_id via
``services.media_intelligence.shot_identity`` (task 14).

Coordinate contract: ``time_seconds`` floats → Edit Source Frames via the
frozen ``round_half_away`` rule (``services.conform.rate_model``). Caller
supplies the edit rate explicitly; no hidden default.

Evidence vs provenance: the MCP analysis store is EVIDENCE ONLY.
``evidence_refs`` carry ``"mcp-analysis:<tool>[:<digest>]"`` pointers;
provider identity lives in ``Shot.provenance`` as ``ProvenanceRecord``.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Annotated

from pydantic import Field, ValidationError

from services.conform.rate_model import round_half_away
from services.contracts.primitives import Identifier, RationalFrameRate, StrictModel
from services.mcp_client.response_normalize import (
    DeepShotAnalysisReport,
    MediaAnalysisStandardReport,
)
from services.media_intelligence.models import (
    EditSourceSpan,
    MediaIntelligenceArtifact,
    MediaSource,
    ProvenanceRecord,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
)
from services.media_intelligence.shot_identity import derive_shot_id


class McpImportError(ValueError):
    """Base MCP import failure."""

    LABEL = "mcp_import_error"


class MissingLineageError(McpImportError):
    """Provenance lineage required for every shot is absent or incomplete."""

    LABEL = "missing_lineage"


class AnalysisLineage(StrictModel):
    """Lineage the caller must supply for every import."""

    provider: Identifier
    provider_version: Identifier
    tool: Identifier
    params_digest: Annotated[str, Field(min_length=1, strict=True)] | None = None
    cost: float | None = None


def _require_lineage(raw: object) -> AnalysisLineage:
    if raw is None:
        raise MissingLineageError(
            "provenance lineage is required: provider, provider_version, tool must be supplied"
        )
    if isinstance(raw, AnalysisLineage):
        return raw
    if isinstance(raw, Mapping):
        try:
            return AnalysisLineage.model_validate(dict(raw))
        except ValidationError as exc:
            details: list[str] = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                err_type = str(err.get("type", ""))
                if err_type == "missing":
                    details.append(f"missing lineage key {loc!r}")
                elif err_type == "extra_forbidden":
                    details.append(f"unknown lineage key {loc!r}")
                else:
                    details.append(f"invalid lineage value at {loc!r} ({err_type})")
            raise MissingLineageError("; ".join(details)) from exc
    got = type(raw).__name__
    raise MissingLineageError(f"provenance must be AnalysisLineage or mapping, got {got}")


def _frame_for_seconds(seconds: float, rate: RationalFrameRate) -> int:
    return round_half_away(Fraction(seconds) * rate.as_fraction)


def _evidence_ref(lineage: AnalysisLineage) -> str:
    base = f"mcp-analysis:{lineage.tool}"
    if lineage.params_digest is not None:
        return f"{base}:{lineage.params_digest}"
    return base


def _lineage_confidence(lineage: AnalysisLineage) -> str:
    parts: list[str] = [f"tool={lineage.tool}"]
    if lineage.params_digest is not None:
        parts.append(f"params_digest={lineage.params_digest}")
    if lineage.cost is not None:
        parts.append(f"cost={lineage.cost}")
    return ":".join(parts)


def _provenance_for_shot(
    lineage: AnalysisLineage,
    *,
    extra_confidence: str | None = None,
) -> tuple[ProvenanceRecord, ...]:
    records: list[ProvenanceRecord] = [
        ProvenanceRecord(
            field="import",
            provider=lineage.provider,
            provider_version=lineage.provider_version,
            confidence=_lineage_confidence(lineage),
        )
    ]
    if extra_confidence is not None:
        records.append(
            ProvenanceRecord(
                field="confidence_extra",
                provider=lineage.provider,
                provider_version=None,
                confidence=extra_confidence,
            )
        )
    return tuple(records)


def _extra_confidence_str(
    *,
    visual: str,
    content: str,
    audio: str,
    editorial: str,
    cuttability: str,
) -> str | None:
    _ = visual
    _ = editorial
    return f"content={content}:audio={audio}:cuttability={cuttability}"


def _map_shot(
    raw: object,
    lineage: AnalysisLineage,
    source_id: str,
    edit_rate: RationalFrameRate,
) -> Shot:
    """Shared mapper for MediaAnalysisShotDescription and DeepShot (same wire shape)."""

    s = _frame_for_seconds(float(raw.time_seconds_start), edit_rate)  # type: ignore[union-attr]
    e = _frame_for_seconds(float(raw.time_seconds_end), edit_rate)  # type: ignore[union-attr]
    e = max(e, s)
    shot_id = derive_shot_id(source_id, s, e, lineage.provider, lineage.provider_version)

    if bool(raw.editorial.best_moment_present) and raw.editorial.best_moment is not None:  # type: ignore[union-attr]
        bm_s = float(raw.editorial.best_moment.time_seconds)  # type: ignore[union-attr]
        bm_frame = _frame_for_seconds(bm_s, edit_rate)
        bm_why: str = str(raw.editorial.best_moment.why)  # type: ignore[union-attr]
    else:
        bm_frame = s
        bm_why = "not reported"

    visual = ShotVisual(
        shot_size=str(raw.visual.shot_size),  # type: ignore[union-attr]
        camera_motion=str(raw.visual.camera_motion),  # type: ignore[union-attr]
        quality_flags=(),
        framing=str(raw.visual.framing),  # type: ignore[union-attr]
    )
    editorial = ShotEditorial(
        role=str(raw.editorial.editorial_role),  # type: ignore[union-attr]
        select_potential=str(raw.editorial.select_potential),  # type: ignore[union-attr]
        best_moment=ShotBestMoment(frame=bm_frame, why=bm_why),
        pacing=str(raw.editorial.pacing),  # type: ignore[union-attr]
        cuttability=ShotCuttability(
            in_=str(raw.cuttability.cut_in.quality),  # type: ignore[union-attr]
            out=str(raw.cuttability.cut_out.quality),  # type: ignore[union-attr]
        ),
        stillness_type=raw.editorial.stillness_type,  # type: ignore[union-attr]
    )
    extra_conf: str | None = _extra_confidence_str(
        visual=str(raw.confidence.visual),  # type: ignore[union-attr]
        content=str(raw.confidence.content),  # type: ignore[union-attr]
        audio=str(raw.confidence.audio),  # type: ignore[union-attr]
        editorial=str(raw.confidence.editorial),  # type: ignore[union-attr]
        cuttability=str(raw.confidence.cuttability),  # type: ignore[union-attr]
    )

    return Shot(
        shot_id=shot_id,
        source_span=EditSourceSpan(start_frame=s, end_frame=e),
        description=str(raw.description),  # type: ignore[union-attr]
        visual=visual,
        editorial=editorial,
        transcript_refs=(),
        evidence_refs=(_evidence_ref(lineage),),
        confidence=ShotConfidence(
            editorial=str(raw.confidence.editorial),  # type: ignore[union-attr]
            visual=str(raw.confidence.visual),  # type: ignore[union-attr]
        ),
        provenance=_provenance_for_shot(lineage, extra_confidence=extra_conf),
    )


def _validate_payload[T: StrictModel](
    payload: object,
    model_type: type[T],
    label: str,
) -> T:
    if isinstance(payload, model_type):
        return payload
    if isinstance(payload, Mapping):
        try:
            return model_type.model_validate(dict(payload))
        except ValidationError as exc:
            raise McpImportError(
                f"{label} payload failed strict validation: "
                + "; ".join(
                    f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('type', '')}"
                    for e in exc.errors()
                )
            ) from exc
    got = type(payload).__name__
    raise McpImportError(f"payload must be {model_type.__name__} or mapping, got {got}")


def import_standard_analysis(
    payload: MediaAnalysisStandardReport | Mapping[str, object],
    *,
    episode_id: str,
    source_id: str,
    edit_rate: RationalFrameRate,
    provenance: AnalysisLineage | Mapping[str, object] | None,
) -> MediaIntelligenceArtifact:
    """Normalize an MCP media_analysis standard-pass payload."""

    lineage = _require_lineage(provenance)
    typed = _validate_payload(payload, MediaAnalysisStandardReport, "standard")
    shots = [_map_shot(d, lineage, source_id, edit_rate) for d in typed.shot_descriptions]
    shots_sorted = sorted(
        shots,
        key=lambda s: (s.source_span.start_frame, s.source_span.end_frame, s.shot_id),
    )
    return MediaIntelligenceArtifact(
        schema_version="media-intelligence-v2",
        episode_id=episode_id,  # type: ignore[arg-type]
        sources=(MediaSource(source_id=source_id),),  # type: ignore[arg-type]
        shots=tuple(shots_sorted),
    )


def import_deep_shots(
    payload: DeepShotAnalysisReport | Mapping[str, object],
    *,
    episode_id: str,
    source_id: str,
    edit_rate: RationalFrameRate,
    provenance: AnalysisLineage | Mapping[str, object] | None,
) -> MediaIntelligenceArtifact:
    """Normalize an MCP deep-shot analysis payload."""

    lineage = _require_lineage(provenance)
    typed = _validate_payload(payload, DeepShotAnalysisReport, "deep")
    shots = [_map_shot(d, lineage, source_id, edit_rate) for d in typed.shots]
    shots_sorted = sorted(
        shots,
        key=lambda s: (s.source_span.start_frame, s.source_span.end_frame, s.shot_id),
    )
    return MediaIntelligenceArtifact(
        schema_version="media-intelligence-v2",
        episode_id=episode_id,  # type: ignore[arg-type]
        sources=(MediaSource(source_id=source_id),),  # type: ignore[arg-type]
        shots=tuple(shots_sorted),
    )


__all__ = [
    "AnalysisLineage",
    "McpImportError",
    "MissingLineageError",
    "import_deep_shots",
    "import_standard_analysis",
]
