"""T6 stage payload documents (the untrusted-data side of the T4 wire).

Every remote stage consumes a canonical-JSON document built ONLY from typed
values — windows, trigger fields, provider payloads, seam facts — serialized
deterministically (sorted keys, fixed separators) and handed to the adapters
as ``untrusted_data``, so no transcript, media, or provider prose can ever
ride the trusted instruction part. ``FusionInputs`` groups the fusion
document's arguments (Smell-2 discipline); the hash helpers feed stage
lineage input/output identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.media_intelligence.budget import DeepReviewWindow
    from services.media_intelligence.moment_review import ReviewWindow
    from services.media_intelligence.video_review_wire import (
        GeminiClipReview,
        GlmClipObservation,
    )
    from services.media_intelligence.video_stage_wire import (
        GeminiEpisodeReduce,
        SpecialistRequestSpan,
    )
    from services.media_intelligence.video_understanding_models import SpecialistGap


def _bounds(window: ReviewWindow) -> dict[str, int]:
    return {
        "start_frame": int(window.start_frame),
        "end_frame": int(window.end_frame),
    }


def _dump(document: dict[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_sha256(document: str) -> str:
    return hashlib.sha256(document.encode()).hexdigest()


def stage_input_sha256(clip_sha256: str, document_sha: str) -> str:
    """Input hash over the exact clip bytes plus the untrusted document."""

    return hashlib.sha256(f"{clip_sha256}\n{document_sha}".encode()).hexdigest()


def local_stage_document(
    episode_id: str, window: ReviewWindow, transcript_ids: Sequence[str], audio_note: str | None
) -> str:
    return _dump(
        {
            "stage": "local_map",
            "episode_id": episode_id,
            "window": _bounds(window),
            "transcript_segment_ids": sorted(transcript_ids),
            "audio_note": audio_note,
        }
    )


def reduce_stage_document(
    episode_id: str,
    local_windows: Sequence[tuple[ReviewWindow, GeminiClipReview]],
    source_duration_frames: int,
) -> str:
    return _dump(
        {
            "stage": "global_reduce",
            "episode_id": episode_id,
            "episode_window": {"start_frame": 0, "end_frame": source_duration_frames},
            "local_results": [
                {
                    "window": _bounds(window),
                    "summary": review.summary,
                    "observations": list(review.observations),
                    "audio_note": review.audio_note,
                }
                for window, review in local_windows
            ],
        }
    )


def specialist_stage_document(
    episode_id: str,
    window: ReviewWindow,
    target: DeepReviewWindow,
    span: SpecialistRequestSpan | None,
    local_summaries: Sequence[str],
) -> str:
    """The GLM target document: trigger fields plus the reduce's OWN rationale
    and uncertainty for this exact range when the target is reduce-derived
    (deterministic progressive targets carry none)."""

    return _dump(
        {
            "stage": "specialist",
            "episode_id": episode_id,
            "window": _bounds(window),
            "trigger_reason": target.trigger_reason,
            "trigger_source": str(target.trigger_source),
            "reduce_rationale": span.rationale if span is not None else None,
            "reduce_uncertainty": span.uncertainty if span is not None else None,
            "local_summaries": list(local_summaries),
        }
    )


@dataclass(frozen=True, slots=True)
class FusionInputs:
    """Everything the fusion document needs, grouped (Smell-2 discipline)."""

    episode_id: str
    window: ReviewWindow
    local: GeminiClipReview
    reduce_result: GeminiEpisodeReduce
    specialists: tuple[tuple[ReviewWindow, GlmClipObservation], ...]
    gaps: tuple[SpecialistGap, ...]
    deferred: tuple[DeepReviewWindow, ...]
    transcript_ids: tuple[str, ...]
    audio_note: str | None


def fusion_stage_document(inputs: FusionInputs) -> str:
    return _dump(
        {
            "stage": "fusion",
            "episode_id": inputs.episode_id,
            "window": _bounds(inputs.window),
            "local": {
                "summary": inputs.local.summary,
                "observations": list(inputs.local.observations),
                "audio_note": inputs.local.audio_note,
            },
            "reduce": {
                "summary": inputs.reduce_result.summary,
                "observations": list(inputs.reduce_result.observations),
                "specialist_requests": [
                    {
                        "start_frame": request.start_frame,
                        "end_frame": request.end_frame,
                        "rationale": request.rationale,
                        "uncertainty": request.uncertainty,
                    }
                    for request in inputs.reduce_result.specialist_requests
                ],
            },
            "specialists": [
                {
                    "window": _bounds(target),
                    "visual_findings": list(observation.visual_findings),
                    "uncertainty": observation.uncertainty,
                }
                for target, observation in inputs.specialists
            ],
            "unresolved": [
                {"start_frame": gap.start_frame, "end_frame": gap.end_frame, "code": gap.code}
                for gap in inputs.gaps
            ],
            "deferred": [
                {
                    "start_frame": int(target.start_frame),
                    "end_frame": int(target.end_frame),
                }
                for target in inputs.deferred
            ],
            "transcript_segment_ids": sorted(inputs.transcript_ids),
            "audio_note": inputs.audio_note,
        }
    )


__all__ = [
    "FusionInputs",
    "document_sha256",
    "fusion_stage_document",
    "local_stage_document",
    "reduce_stage_document",
    "specialist_stage_document",
    "stage_input_sha256",
]
