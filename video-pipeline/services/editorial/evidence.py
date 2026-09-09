"""Bounded evidence assembly for the Editorial Director (Todo-37 surface only).

The Director holds NO duckdb/shell/file handle: evidence arrives exclusively
through typed ``MediaQueryApi`` calls (the frozen 7-method allowlist). Every
declared candidate must be corroborated by indexed evidence — text-bearing
candidates by an exact-substring transcript search hit, empty-text pause
candidates by an overlapping silence range, and empty-text speech-derived
candidates (filler, false_start — the analyzer derives their span FROM a
transcript segment, so the transcript is the searchable record for "something
was said here") by containment of their frame span inside an indexed
transcript segment's span — and every contributing row's ``artifact_sha`` is
collected into the evidence lineage that keys the transport request hash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.media_query.api_models import (
    FROZEN_MAX_PAGE_SIZE,
    FROZEN_ROW_BUDGET,
    ApiBudgetExceeded,
    EpisodeSummaryRequest,
    Pagination,
    SampleSpan,
    SearchTranscriptsRequest,
    SilenceHitRow,
    SilenceRangesRequest,
    TranscriptHitRow,
)
from services.media_query.row_models import AUDIO_SOURCE_ID

if TYPE_CHECKING:
    from services.editorial.models import DeclaredCandidate, DirectorRequest
    from services.media_query.api import MediaQueryApi

_TEXT_QUERY_MAX = 200

# Kinds whose span the analyzer derives FROM a transcript segment
# (``generate_filler_candidates``: ``span=segment.span``;
# ``generate_false_start_candidates``: ``span=abandoned.span``) — a span of
# this kind that no transcript segment covers is a broken declaration.
_SPEECH_DERIVED_KINDS: Final = ("filler", "false_start")

# r5: the real-footage silence index holds 256 rows while the frozen page is
# 50 — a single first-page fetch renders corroborating rows past row 50
# deterministically invisible. Page full-span queries to exhaustion. The
# bound is derived from the frozen contract (every page window must stay
# within FROZEN_ROW_BUDGET), so memory is bounded and single-page indexes
# issue exactly the same first request as before.
_EVIDENCE_MAX_PAGES: Final = FROZEN_ROW_BUDGET // FROZEN_MAX_PAGE_SIZE


class EvidenceIncomplete(Exception):  # noqa: N818 (outcome category, mirrors ApiBudgetExceeded)
    """The bounded evidence bundle cannot corroborate the declared inputs."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Corroboration(StrictModel):
    segment_id: str
    method: Literal["transcript_search", "silence_overlap", "transcript_containment"]


class EvidenceBundle(StrictModel):
    """Bounded, lineage-complete evidence view; proposals may cite only these."""

    source_rows: int = Field(ge=0, strict=True)
    lineage: tuple[str, ...]
    admissible_segment_ids: tuple[str, ...]
    corroborations: tuple[Corroboration, ...]


def _total_samples(request: DirectorRequest) -> int:
    source = request.edit_source
    return (
        source.total_frames
        * source.audio_sample_rate
        * source.frame_rate_den
        // source.frame_rate_num
    )


def _candidate_samples(request: DirectorRequest, frame: int) -> int:
    source = request.edit_source
    return frame * source.audio_sample_rate * source.frame_rate_den // source.frame_rate_num


def _ms_start_frame(request: DirectorRequest, ms: int) -> int:
    source = request.edit_source
    return ms * source.frame_rate_num // (source.frame_rate_den * 1000)


def _ms_end_frame(request: DirectorRequest, ms: int) -> int:
    source = request.edit_source
    return (ms * source.frame_rate_num + source.frame_rate_den * 1000 - 1) // (
        source.frame_rate_den * 1000
    )


def _fetch_all_transcript_hits(
    api: MediaQueryApi, text_query: str
) -> tuple[TranscriptHitRow, ...]:
    collected: list[TranscriptHitRow] = []
    offset = 0
    while True:
        try:
            page = api.search_transcripts(
                SearchTranscriptsRequest(
                    source_id=AUDIO_SOURCE_ID,
                    text_query=text_query,
                    pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=offset),
                )
            )
        except ApiBudgetExceeded as error:
            raise EvidenceIncomplete(error.detail) from error
        collected.extend(page.rows)
        if len(collected) >= page.total or not page.rows:
            return tuple(collected)
        offset += FROZEN_MAX_PAGE_SIZE
        if offset // FROZEN_MAX_PAGE_SIZE >= _EVIDENCE_MAX_PAGES:
            raise EvidenceIncomplete(
                "transcript evidence for the containment query spans more than the "
                f"frozen {_EVIDENCE_MAX_PAGES}-page bound "
                f"({_EVIDENCE_MAX_PAGES * FROZEN_MAX_PAGE_SIZE} rows); "
                "narrow the episode span or split the editorial request"
            )


def _corroborate_speech_derived(
    api: MediaQueryApi,
    request: DirectorRequest,
    candidate: DeclaredCandidate,
    lineage: set[str],
) -> Corroboration:
    covers = [
        cover
        for cover in request.candidates
        if cover.text
        and cover.start_frame <= candidate.start_frame
        and candidate.end_frame <= cover.end_frame
    ]
    if not covers:
        raise EvidenceIncomplete(
            f"declared {candidate.kind} candidate {candidate.segment_id} span "
            f"[{candidate.start_frame},{candidate.end_frame}) lies outside every "
            "text-bearing declaration in the request; refusing to propose over "
            "uncorroborated declarations"
        )
    for cover in covers:
        hits = _fetch_all_transcript_hits(api, cover.text)
        for hit in hits:
            if (
                _ms_start_frame(request, hit.span.start_ms) <= candidate.start_frame
                and candidate.end_frame <= _ms_end_frame(request, hit.span.end_ms)
            ):
                lineage.update(row.artifact_sha for row in hits)
                return Corroboration(
                    segment_id=candidate.segment_id, method="transcript_containment"
                )
    raise EvidenceIncomplete(
        f"declared {candidate.kind} candidate {candidate.segment_id} span is not "
        "contained in any indexed transcript segment; refusing to propose over "
        "uncorroborated declarations"
    )


def _fetch_all_silence_rows(
    api: MediaQueryApi, request: DirectorRequest
) -> tuple[SilenceHitRow, ...]:
    span = SampleSpan(start_sample=0, end_sample=_total_samples(request))
    collected: list[SilenceHitRow] = []
    offset = 0
    while True:
        try:
            page = api.silence_ranges(
                SilenceRangesRequest(
                    source_id=AUDIO_SOURCE_ID,
                    span=span,
                    pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=offset),
                )
            )
        except ApiBudgetExceeded as error:
            raise EvidenceIncomplete(error.detail) from error
        collected.extend(page.rows)
        if len(collected) >= page.total or not page.rows:
            return tuple(collected)
        offset += FROZEN_MAX_PAGE_SIZE
        if offset // FROZEN_MAX_PAGE_SIZE >= _EVIDENCE_MAX_PAGES:
            raise EvidenceIncomplete(
                "silence evidence spans more than the frozen "
                f"{_EVIDENCE_MAX_PAGES}-page bound "
                f"{_EVIDENCE_MAX_PAGES * FROZEN_MAX_PAGE_SIZE} rows; "
                "narrow the episode span or split the editorial request"
            )


def assemble_evidence(api: MediaQueryApi, request: DirectorRequest) -> EvidenceBundle:
    summary = api.episode_summary(EpisodeSummaryRequest(source_id=AUDIO_SOURCE_ID))
    if summary.total == 0:
        raise EvidenceIncomplete(
            f"no indexed sources for {AUDIO_SOURCE_ID}; the episode has no analyzable "
            "evidence for the editorial stage"
        )
    lineage = {row.artifact_sha for row in summary.rows}
    silence_rows = _fetch_all_silence_rows(api, request)
    lineage.update(row.artifact_sha for row in silence_rows)

    corroborations: list[Corroboration] = []
    for candidate in request.candidates:
        if candidate.text:
            if len(candidate.text) > _TEXT_QUERY_MAX:
                raise EvidenceIncomplete(
                    f"declared candidate {candidate.segment_id} text exceeds the frozen "
                    f"{_TEXT_QUERY_MAX}-character query bound"
                )
            try:
                hits = api.search_transcripts(
                    SearchTranscriptsRequest(
                        source_id=AUDIO_SOURCE_ID,
                        text_query=candidate.text,
                        pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=0),
                    )
                )
            except ApiBudgetExceeded as error:
                raise EvidenceIncomplete(error.detail) from error
            if hits.total == 0:
                raise EvidenceIncomplete(
                    f"declared candidate {candidate.segment_id} text is absent from the "
                    "indexed transcript evidence; refusing to propose over uncorroborated "
                    "declarations"
                )
            lineage.update(row.artifact_sha for row in hits.rows)
            corroborations.append(
                Corroboration(segment_id=candidate.segment_id, method="transcript_search")
            )
            continue
        if candidate.kind == "pause":
            start = _candidate_samples(request, candidate.start_frame)
            end = _candidate_samples(request, candidate.end_frame)
            overlapping = [
                row
                for row in silence_rows
                if row.span.start_sample < end and row.span.end_sample > start
            ]
            if not overlapping:
                raise EvidenceIncomplete(
                    f"declared pause candidate {candidate.segment_id} has no overlapping "
                    "silence evidence in the index"
                )
            corroborations.append(
                Corroboration(segment_id=candidate.segment_id, method="silence_overlap")
            )
            continue
        if candidate.kind not in _SPEECH_DERIVED_KINDS:
            raise EvidenceIncomplete(
                f"declared candidate {candidate.segment_id} has no searchable text and no "
                "silence-corroborable kind"
            )
        corroborations.append(
            _corroborate_speech_derived(api, request, candidate, lineage)
        )
    return EvidenceBundle(
        source_rows=summary.total,
        lineage=tuple(sorted(lineage)),
        admissible_segment_ids=tuple(
            candidate.segment_id for candidate in request.candidates
        ),
        corroborations=tuple(corroborations),
    )


__all__ = [
    "Corroboration",
    "EvidenceBundle",
    "EvidenceIncomplete",
    "assemble_evidence",
]
