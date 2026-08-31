"""Reconcile local deterministic evidence with MCP-imported shots.

- Local facts (black/blur/exposure/scene boundaries/loudness/silence)
  are provided via the input-adapter models in this package — this module
  never imports or mutates the analyzer internals beyond reading their
  types as data.
- MCP shots are the canonical ``MediaIntelligenceArtifact`` from Task 12/13.
- Boundary conflicts (local scene-change boundary vs MCP shot boundary
  beyond tolerance) → **both** evidences retained with an explicit
  ``ProvenanceRecord(field="boundary_conflict")`` on the affected shot;
  never silently merged.
- Non-conflicting measurements attach as optional evidence fields.
- Determinism: no timestamps/random/uuid; byte-identical output for
  identical inputs (stable sort, stable hash IDs).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Frame, Identifier, SourceId, StrictModel, to_tuple
from services.media_intelligence.models import (
    AudioMeasurements,
    EditSourceSpan,
    MediaIntelligenceArtifact,
    ProvenanceRecord,
    Shot,
    SilenceSegment,
    VisualQualityDetail,
)
from services.media_intelligence.shot_identity import (
    UnknownSourceError,
    derive_shot_id,
    ensure_source_known,
)

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class ReconcileError(ValueError):
    """Base reconcile failure."""

    LABEL = "reconcile_error"


class UnknownSourceReconcileError(ReconcileError, UnknownSourceError):
    """Reconcile typed rejection for unknown source_id."""

    LABEL = "unknown_source"


# ---------------------------------------------------------------------------
# Input adapters (local deterministic evidence)
# ---------------------------------------------------------------------------


class LocalSourceEvidence(StrictModel):
    """Per-source deterministic facts (adapter, not the analyzer model)."""

    source_id: SourceId
    scene_boundaries: Annotated[tuple[Frame, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    black_spans: Annotated[tuple[EditSourceSpan, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    blur_spans: Annotated[tuple[EditSourceSpan, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    exposure_spans: Annotated[tuple[EditSourceSpan, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    silence_segments: Annotated[
        tuple[SilenceSegment, ...], BeforeValidator(to_tuple)
    ] = Field(default_factory=tuple)
    audio_measurements: AudioMeasurements | None = None


class LocalEvidenceBatch(StrictModel):
    """Batch of per-source local facts merged against one episode."""

    sources: Annotated[tuple[LocalSourceEvidence, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )


class ConformContext(StrictModel):
    """Allowed source_ids from the Conform Map / Source Manifest world."""

    source_ids: Annotated[tuple[SourceId, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )

    @property
    def allowed_set(self) -> frozenset[str]:
        return frozenset(self.source_ids)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _derive_source_id_for_shot(
    mcp_artifact: MediaIntelligenceArtifact,
    conform: ConformContext,
) -> str:
    if mcp_artifact.sources:
        return str(mcp_artifact.sources[0].source_id)
    if conform.source_ids:
        return str(conform.source_ids[0])
    raise ReconcileError("no source_id available to derive shot identity")


def _collect_conflicts(
    local_boundaries: list[int],
    mcp_boundaries: list[int],
    tolerance: int,
) -> list[tuple[int, int, int]]:
    conflicts: list[tuple[int, int, int]] = []
    if not mcp_boundaries:
        return conflicts
    for lb in local_boundaries:
        nearest = min(mcp_boundaries, key=lambda mb: abs(mb - lb))
        dist = abs(nearest - lb)
        if dist > tolerance:
            conflicts.append((lb, nearest, dist))
    return conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile(  # noqa: C901, PLR0912, PLR0915
    local_evidence: LocalEvidenceBatch,
    mcp_artifact: MediaIntelligenceArtifact,
    conform_context: ConformContext,
    *,
    boundary_tolerance_frames: int = 2,
) -> MediaIntelligenceArtifact:
    """Merge local facts and MCP shots into one canonical artifact.

    Raises ``UnknownSourceReconcileError`` when any referenced
    ``source_id`` is absent from ``conform_context``.
    """

    if boundary_tolerance_frames < 0:
        raise ReconcileError("boundary_tolerance_frames must be >= 0")

    allowed = conform_context.allowed_set

    # --- conform validation ---
    for src in mcp_artifact.sources:
        if str(src.source_id) not in allowed:
            msg = f"unknown source_id in MCP artifact: {src.source_id!r}"
            raise UnknownSourceReconcileError(msg)
    for local_src in local_evidence.sources:
        if str(local_src.source_id) not in allowed:
            raise UnknownSourceReconcileError(
                f"unknown source_id in local evidence: {local_src.source_id!r}"
            )
        # also validate via shot_identity helper for consistent LABEL
        try:
            ensure_source_known(str(local_src.source_id), set(allowed))
        except UnknownSourceError as exc:
            raise UnknownSourceReconcileError(str(exc)) from exc

    # --- prepare boundaries (deterministic order) ---
    mcp_shots_sorted = sorted(
        mcp_artifact.shots,
        key=lambda s: (s.source_span.start_frame, s.source_span.end_frame, s.shot_id),
    )
    mcp_boundaries: list[int] = []
    if len(mcp_shots_sorted) > 1:
        mcp_boundaries.extend(
            int(shot.source_span.start_frame) for shot in mcp_shots_sorted[1:]
        )

    local_boundaries: list[int] = []
    for src in sorted(local_evidence.sources, key=lambda s: str(s.source_id)):
        local_boundaries.extend(int(b) for b in sorted(src.scene_boundaries))

    conflicts = _collect_conflicts(local_boundaries, mcp_boundaries, boundary_tolerance_frames)
    # map MCP boundary -> list of (local_boundary, dist)
    conflict_by_mcp: dict[int, list[tuple[int, int]]] = {}
    for lb, mb, dist in conflicts:
        conflict_by_mcp.setdefault(mb, []).append((lb, dist))
    conflict_local_set = {lb for lb, _, _ in conflicts}

    # infer primary source_id for deterministic ID derivation
    primary_source_id = _derive_source_id_for_shot(mcp_artifact, conform_context)
    try:
        ensure_source_known(primary_source_id, set(allowed))
    except UnknownSourceError as exc:
        raise UnknownSourceReconcileError(str(exc)) from exc

    # collect union of local spans/measurements for attachment
    all_local_silence: list[SilenceSegment] = []
    all_local_audio: AudioMeasurements | None = None
    all_local_black: list[EditSourceSpan] = []
    all_local_blur: list[EditSourceSpan] = []
    all_local_exposure: list[EditSourceSpan] = []
    for src in local_evidence.sources:
        all_local_silence.extend(list(src.silence_segments))
        all_local_black.extend(list(src.black_spans))
        all_local_blur.extend(list(src.blur_spans))
        all_local_exposure.extend(list(src.exposure_spans))
        if src.audio_measurements is not None and all_local_audio is None:
            all_local_audio = src.audio_measurements

    # --- build merged shots ---
    merged_shots: list[Shot] = []
    for shot in mcp_shots_sorted:
        # deterministic ID (hash of source_id + conform-checked span + provider)
        derived_id: Identifier = derive_shot_id(
            primary_source_id,
            int(shot.source_span.start_frame),
            int(shot.source_span.end_frame),
            "mcp",
        )

        # start from existing provenance
        provenance: list[ProvenanceRecord] = list(shot.provenance or [])

        # boundary conflict record for this shot's start boundary
        start_frame = int(shot.source_span.start_frame)
        if start_frame in conflict_by_mcp:
            for lb, dist in sorted(conflict_by_mcp[start_frame]):
                provenance.append(
                    ProvenanceRecord(
                        field="boundary_conflict",
                        provider="reconcile",
                        provider_version=None,
                        confidence=f"local={lb}:mcp={start_frame}:dist={dist}",
                    )
                )
            # retain local boundary evidence as well (second provenance entry)
            for lb, _dist in sorted(conflict_by_mcp[start_frame]):
                provenance.append(
                    ProvenanceRecord(
                        field="boundary_conflict_local_boundary",
                        provider="local",
                        provider_version=None,
                        confidence=str(lb),
                    )
                )

        # if this shot is the first and its span is NOT a MCP boundary,
        # but local boundaries conflict elsewhere, no per-shot record needed
        # — global conflicts are already recorded on their MCP-boundary shots.

        # attach non-conflicting local measurements
        # Strategy: overlapping spans attach; if no overlap but we have global
        # measurements, attach them once to the first shot or to overlapping.
        shot_start = int(shot.source_span.start_frame)
        shot_end = int(shot.source_span.end_frame)

        overlapping_silence = tuple(
            seg
            for seg in all_local_silence
            if _spans_overlap(int(seg.start_frame), int(seg.end_frame), shot_start, shot_end)
        )
        # if no overlapping but we have silence, keep empty to preserve determinism?
        # only attach when overlapping or when shot is the sole shot and silence exists
        effective_silence: tuple[SilenceSegment, ...] | None = shot.silence_segments
        if effective_silence is None and overlapping_silence:
            effective_silence = overlapping_silence
        elif effective_silence is None and not overlapping_silence and all_local_silence and len(
            mcp_shots_sorted
        ) == 1:
            # single-shot episode: all silence belongs to the sole shot
            effective_silence = tuple(all_local_silence)

        # audio measurements: attach if shot has none and local has one
        effective_audio = shot.audio_measurements
        if effective_audio is None and all_local_audio is not None:
            # attach to shots whose span overlaps local evidence span?
            # for determinism, attach to every shot without audio when local exists
            effective_audio = all_local_audio

        # visual quality: if shot has no VisualQualityDetail and local has quality spans overlapping
        effective_quality_detail = shot.visual_quality_detail
        # if any black/blur/exposure overlaps this shot, synthesize a detail
        overlapping_quality = [
            span
            for span in (*all_local_black, *all_local_blur, *all_local_exposure)
            if _spans_overlap(int(span.start_frame), int(span.end_frame), shot_start, shot_end)
        ]
        if overlapping_quality and not shot.visual_quality_detail:
            effective_quality_detail = VisualQualityDetail(
                issue="local_quality_flag", severity=None
            )

        # evidence_refs: append markers for local retention when conflicts exist
        evidence_refs: list[str] = list(shot.evidence_refs or [])
        if conflict_local_set:
            for lb in sorted(conflict_local_set):
                marker = f"local-boundary:{lb}"
                if marker not in evidence_refs:
                    evidence_refs.append(marker)
        # preserve local span evidence markers deterministically
        if all_local_black or all_local_blur or all_local_exposure:
            local_marker = "local-quality:present"
            if local_marker not in evidence_refs and effective_quality_detail is not None:
                evidence_refs.append(local_marker)

        # sort evidence_refs for determinism while preserving original order first?
        # we appended deterministically sorted markers, keep order stable
        # provenance already deterministically appended

        # ensure provenance tuple is sorted? Keep insertion order but deterministic
        # already deterministic due to sorted inputs

        merged_shot = shot.model_copy(
            update={
                "shot_id": derived_id,
                "silence_segments": effective_silence,
                "audio_measurements": effective_audio,
                "visual_quality_detail": effective_quality_detail,
                "provenance": tuple(provenance) if provenance else None,
                "evidence_refs": tuple(evidence_refs),
            }
        )
        merged_shots.append(merged_shot)

    # --- final artifact: deterministic ordering ---
    merged_shots_sorted = sorted(
        merged_shots, key=lambda s: (s.source_span.start_frame, s.source_span.end_frame, s.shot_id)
    )

    # also ensure local conflicts are globally visible even if no shot matches?
    # if conflicts exist but no mcp boundary matches (e.g., single-shot),
    # attach a conflict provenance to the sole shot
    if conflicts and not conflict_by_mcp and merged_shots_sorted:
        sole = merged_shots_sorted[0]
        provenance = list(sole.provenance or [])
        for lb, mb, dist in sorted(conflicts):
            provenance.append(
                ProvenanceRecord(
                    field="boundary_conflict",
                    provider="reconcile",
                    provider_version=None,
                    confidence=f"local={lb}:mcp={mb}:dist={dist}",
                )
            )
        evidence_refs = list(sole.evidence_refs or [])
        for lb, _, _ in sorted(conflicts):
            marker = f"local-boundary:{lb}"
            if marker not in evidence_refs:
                evidence_refs.append(marker)
        merged_shots_sorted[0] = sole.model_copy(
            update={"provenance": tuple(provenance), "evidence_refs": tuple(evidence_refs)}
        )

    return MediaIntelligenceArtifact(
        schema_version=mcp_artifact.schema_version,
        episode_id=mcp_artifact.episode_id,
        sources=mcp_artifact.sources,
        shots=tuple(merged_shots_sorted),
    )
