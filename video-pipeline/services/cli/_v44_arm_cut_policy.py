"""Deterministic removal-eligibility computation for the Arm pipeline (T4).

The eligibility is computed IN CODE from the EFFECTIVE speech sequence (the
Task-3 lane output — never stale system ASR text): ``false_start`` reuses the
frozen analyzer rule verbatim (:func:`generate_false_start_candidates` over a
rebuilt ``TranscriptSpanMap`` of the same spans), and ``exact_duplicate`` is
byte-equality of ADJACENT speech after the shared Japanese normalization
(:func:`normalize_duplicate_text`). The later duplicate occurrence is the
removable one; the first stays. Output is runtime-only — never persisted as
an artifact.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Final

from services.analyze.analysis_models import SampleMsSpan
from services.analyze.audio_constants import ANALYZER_VERSION
from services.analyze.candidate_models import (
    FalseStartEvidence,
    MappedTranscriptSegment,
    TranscriptSpanMap,
)
from services.analyze.candidates import generate_false_start_candidates
from services.editorial_v2.removal_policy import (
    RemovalEligibilityV1,
    RemovalReason,
    normalize_duplicate_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.cli.real_pool import SpeechSegment

RATE_NUM: Final = 30
RATE_HZ: Final = 48_000


def _ms(frame: int) -> int:
    return frame * 1000 // RATE_NUM


def _span_map(speech: Sequence[SpeechSegment]) -> TranscriptSpanMap:
    return TranscriptSpanMap(
        sample_rate=RATE_HZ,
        segments=tuple(
            MappedTranscriptSegment(
                index=position,
                text=segment.text,
                is_speech=True,
                span=SampleMsSpan(
                    start_sample=_ms(segment.start_frame) * RATE_HZ // 1000,
                    end_sample=_ms(segment.end_frame) * RATE_HZ // 1000,
                    sample_rate=RATE_HZ,
                    start_ms=_ms(segment.start_frame),
                    end_ms=_ms(segment.end_frame),
                ),
            )
            for position, segment in enumerate(speech)
        ),
    )


def compute_removal_eligibility(
    speech: Sequence[SpeechSegment], transcript_sha256: str
) -> tuple[RemovalEligibilityV1, ...]:
    """One deny-by-default entry per speech candidate, in speech order."""
    allowed: dict[str, set[RemovalReason]] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    for candidate in generate_false_start_candidates(
        _span_map(speech), ANALYZER_VERSION, (transcript_sha256,)
    ):
        if not isinstance(candidate.evidence, FalseStartEvidence):
            continue
        abandoned = speech[candidate.evidence.abandoned_segment_index]
        restart = speech[candidate.evidence.restart_segment_index]
        cid = f"cand-{abandoned.segment_id}"
        allowed.setdefault(cid, set()).add("false_start")
        evidence[cid] = (abandoned.segment_id, restart.segment_id)
    for first, second in pairwise(speech):
        normalized = normalize_duplicate_text(first.text)
        if normalized and normalized == normalize_duplicate_text(second.text):
            cid = f"cand-{second.segment_id}"
            allowed.setdefault(cid, set()).add("exact_duplicate")
            evidence[cid] = (first.segment_id, second.segment_id)
    return tuple(
        RemovalEligibilityV1(
            candidate_id=cid,
            allowed_reasons=frozenset(allowed.get(cid, ())),
            evidence_refs=evidence.get(cid, ()),
        )
        for cid in (f"cand-{segment.segment_id}" for segment in speech)
    )


__all__ = ["compute_removal_eligibility"]
