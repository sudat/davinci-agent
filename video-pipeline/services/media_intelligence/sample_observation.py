"""GLM full-source chunked observation, pure half (PRD v4.4 §8.5).

Partitioning, transcript slicing, typed observations, the local candidate
pick, the journal record, and candidate→window resolution. The scout wire
lives in ``sample_observation_wire``; ``api_consultation`` assembles the
server path around these two halves.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from math import ceil
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.compile.sample_projection import windows_around_anchors
from services.contracts.primitives import RecordFrameSpan, StrictModel, to_tuple

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C

SAMPLE_OBSERVATION_CHUNK_SECONDS: Final = 60.0
MAX_SAMPLE_CANDIDATES: Final = 3


class SampleObservationError(Exception):
    """Typed fail-closed observation failure (code + detail, no fallback)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _to_float(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


type Second = Annotated[float, BeforeValidator(_to_float)]
type NonEmptyText = Annotated[str, Field(min_length=1, strict=True)]


def _to_ranges(value: object) -> object:
    if isinstance(value, list):
        return tuple(tuple(item) if isinstance(item, list) else item for item in value)
    return value


class SampleChunk(StrictModel):
    """One contiguous partition of ``[0, source_duration)`` in seconds."""

    index: int = Field(ge=0)
    start_seconds: Second = Field(ge=0.0)
    end_seconds: Second = Field(gt=0.0)


class SampleCandidate(StrictModel):
    """One GLM-proposed sample place: second-precision range + reason."""

    start_second: Second = Field(ge=0.0)
    end_second: Second = Field(ge=0.0)
    reason: NonEmptyText


class SampleChunkObservation(StrictModel):
    """Typed GLM observation over exactly one chunk (prompt-driven stamps)."""

    chunk_index: int = Field(ge=0)
    chunk_start_seconds: Second = Field(ge=0.0)
    chunk_end_seconds: Second = Field(gt=0.0)
    findings: Annotated[tuple[NonEmptyText, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )
    scene_changes: Annotated[tuple[tuple[Second, Second], ...], BeforeValidator(_to_ranges)] = ()
    candidates: Annotated[tuple[SampleCandidate, ...], BeforeValidator(to_tuple)] = ()
    uncertainty: str | None = None
    quality_insufficient: bool = False
    insufficiency_note: str | None = None

    @model_validator(mode="after")
    def stamps_within_chunk(self) -> SampleChunkObservation:
        start, end = self.chunk_start_seconds, self.chunk_end_seconds
        if end <= start:
            raise PydanticCustomError(
                "sample-observation-chunk-empty", "chunk span must be forward"
            )
        for change_start, change_end in self.scene_changes:
            if not start <= change_start <= change_end <= end:
                raise PydanticCustomError(
                    "sample-observation-range-outside-chunk",
                    "scene-change ranges must lie inside the chunk",
                )
        for candidate in self.candidates:
            c = candidate
            if not (start <= c.start_second <= c.end_second <= end):
                raise PydanticCustomError(
                    "sample-observation-candidate-outside-chunk",
                    "candidate ranges must lie inside the chunk",
                )
        return self


def partition_chunks(
    duration_seconds: float,
    chunk_seconds: float = SAMPLE_OBSERVATION_CHUNK_SECONDS,
) -> tuple[SampleChunk, ...]:
    """Contiguous ``[0, duration)`` chunks: no overlap, no gap (last shorter)."""

    if not duration_seconds > 0:
        raise SampleObservationError(
            "sample-observation-unavailable",
            "the edit source reports no duration; cannot partition chunks",
        )
    chunks: list[SampleChunk] = []
    start, index = 0.0, 0
    while start < duration_seconds:
        end = min(start + chunk_seconds, duration_seconds)
        chunks.append(SampleChunk(index=index, start_seconds=start, end_seconds=end))
        start, index = end, index + 1
    return tuple(chunks)


def chunk_frame_range(
    chunk: SampleChunk, fps: Fraction, total_frames: int
) -> tuple[int, int]:
    """Exact rational seconds→frames (floor in, ceil out), clamped to source."""

    start = max(int(Fraction(chunk.start_seconds) * fps), 0)
    end = min(ceil(Fraction(chunk.end_seconds) * fps), total_frames)
    if end <= start:
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"chunk {chunk.index} maps to an empty frame range",
        )
    return start, end


def slice_transcript(
    segments: Sequence[tuple[float, float, str]], chunk: SampleChunk
) -> str:
    """Overlapping segment texts for one chunk, joined in time order."""

    return " ".join(
        text.strip()
        for start, end, text in segments
        if end > chunk.start_seconds and start < chunk.end_seconds and text.strip()
    )


def pick_candidate_anchors(
    observations: Sequence[SampleChunkObservation],
    limit: int = MAX_SAMPLE_CANDIDATES,
) -> tuple[float, ...]:
    """Local deterministic choice: up to ``limit`` non-duplicate-content
    candidate midpoints, spread across chunks first (one per chunk per
    round), leftovers filling in chunk order. Insufficient chunks skipped."""

    viable: list[list[float]] = []
    seen: set[str] = set()
    for observation in sorted(observations, key=lambda o: o.chunk_index):
        if observation.quality_insufficient:
            continue
        kept: list[float] = []
        for candidate in observation.candidates:
            key = " ".join(candidate.reason.split()).casefold()
            if key not in seen:
                seen.add(key)
                kept.append((candidate.start_second + candidate.end_second) / 2.0)
        if kept:
            viable.append(kept)
    rounds = max((len(kept) for kept in viable), default=0)
    anchors: list[float] = []
    for round_index in range(rounds):
        for kept in viable:
            if round_index < len(kept):
                anchors.append(kept[round_index])
                if len(anchors) >= limit:
                    return tuple(anchors)
    return tuple(anchors)


def observation_record(
    chunks: Sequence[SampleChunk],
    observations: Sequence[SampleChunkObservation],
    anchors_seconds: Sequence[float],
) -> dict[str, object]:
    """Journal-ready record: per-chunk findings/candidates plus the
    GLM-quality-insufficient chunk list (metered assist stays gated)."""

    by_index = {o.chunk_index: o for o in observations}
    entries: list[dict[str, object]] = []
    for chunk in chunks:
        observation = by_index.get(chunk.index)
        entries.append(
            {
                "index": chunk.index,
                "start_seconds": chunk.start_seconds,
                "end_seconds": chunk.end_seconds,
                "findings": list(observation.findings) if observation else [],
                "scene_changes": (
                    [list(p) for p in observation.scene_changes] if observation else []
                ),
                "candidates": (
                    [
                        {
                            "start_second": c.start_second,
                            "end_second": c.end_second,
                            "reason": c.reason,
                        }
                        for c in observation.candidates
                    ]
                    if observation
                    else []
                ),
                "uncertainty": observation.uncertainty if observation else None,
                "quality_insufficient": (
                    observation.quality_insufficient if observation else True
                ),
                "insufficiency_note": (
                    observation.insufficiency_note if observation else "unobserved"
                ),
            }
        )
    return {
        "chunk_seconds": SAMPLE_OBSERVATION_CHUNK_SECONDS,
        "chunks": entries,
        "insufficient_chunks": [e["index"] for e in entries if e["quality_insufficient"]],
        "anchors_seconds": list(anchors_seconds),
    }


def resolve_observed_windows(
    *,
    full_ir: TimelineIr0C,
    source_rate: Fraction,
    observations: Sequence[SampleChunkObservation],
    limit_seconds: float = 30.0,
) -> tuple[RecordFrameSpan, ...]:
    """Candidate seconds → source frames (exact rational) → record anchors
    through the committed video track's source map → widened windows. A
    candidate landing on no video item is skipped, never clamped."""

    anchors = pick_candidate_anchors(observations)
    if not anchors:
        raise SampleObservationError(
            "sample-observation-insufficient",
            "the GLM chunk observations carry no usable sample candidate; "
            "refusing rather than falling back to position sampling",
        )
    video = next((t for t in full_ir.tracks if t.track.kind == "video"), None)
    if video is None or not video.items:
        raise SampleObservationError(
            "sample-observation-unavailable",
            "the committed plan carries no video track for sample anchors",
        )
    items = sorted(video.items, key=lambda i: i.source.span.start_frame)
    mapped: list[int] = []
    for second in anchors:
        frame = int(Fraction(second) * source_rate)
        hit = next(
            (
                item
                for item in items
                if item.source.span.start_frame <= frame < item.source.span.end_frame
            ),
            None,
        )
        if hit is None:
            continue
        mapped.append(
            hit.record_span.start_frame + (frame - hit.source.span.start_frame)
        )
    if not mapped:
        raise SampleObservationError(
            "sample-observation-insufficient",
            "no candidate maps onto the committed video track; refusing",
        )
    return windows_around_anchors(full_ir, mapped, limit_seconds)


__all__ = [
    "MAX_SAMPLE_CANDIDATES", "SAMPLE_OBSERVATION_CHUNK_SECONDS", "SampleCandidate",
    "SampleChunk", "SampleChunkObservation", "SampleObservationError",
    "chunk_frame_range", "observation_record", "partition_chunks",
    "pick_candidate_anchors", "resolve_observed_windows", "slice_transcript",
]
