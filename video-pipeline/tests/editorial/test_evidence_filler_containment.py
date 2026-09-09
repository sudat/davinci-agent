"""r6 regression: empty-text speech-derived candidates get transcript containment.

Real footage (r6 ``ep-043e1e769957f8c1``) declared filler ``fi127`` — a 「えー」
lexicon match inside transcript segment 「えーと」 at 135.72-138.08s — with EMPTY
text, and ``assemble_evidence`` had no corroboration path for it: text-bearing
candidates search transcripts, pauses overlap silence, everything else stopped.
The analyzer derives a filler's span FROM the transcript segment
(``span=segment.span``) and a false_start's from the abandoned speech segment,
so the honest basis is containment of the candidate's frame span inside an
indexed transcript segment's span. The pinned r6 shape: 30fps lattice,
ms→frame floor-start/ceil-end — hit ms (135720, 138080) ↔ speech [4071, 4143)
↔ filler [4071, 4142).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, cast

import pytest

from services.editorial.evidence import (
    EvidenceBundle,
    EvidenceIncomplete,
    assemble_evidence,
)
from services.editorial.models import DeclaredCandidate, DirectorRequest
from services.media_query import api_models as m
from services.media_query.row_models import AUDIO_SOURCE_ID
from tests.editorial.support import load_manifest, manifest_request

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.media_query.api import MediaQueryApi

R6_SEGMENT_START_MS = 135720
R6_SEGMENT_END_MS = 138080
R6_SPEECH_START_FRAME = 4071  # floor(135720 * 30 / 1000), lattice 3
R6_SPEECH_END_FRAME = 4143  # ceil(138080 * 30 / 1000), lattice 3
R6_FILLER_END_FRAME = 4142  # floor(138080 * 48 * 30 / 48000) — real record shape
# r7: the speech-segmentation lattice constant (services/cli/real_pool.py:51)
# collapses adjacent segment boundaries when the residue is <= LATTICE, so the
# real s51 declaration starts at 4074 — exactly LATTICE frames after the
# sample-derived filler start 4071.
R7_LATTICE = 3
R7_SHIFTED_SPEECH_START_FRAME = R6_SPEECH_START_FRAME + R7_LATTICE  # 4074


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _candidate(
    segment_id: str,
    kind: Literal["speech", "pause", "filler", "false_start"],
    text: str,
    start_frame: int = R6_SPEECH_START_FRAME,
    end_frame: int = R6_FILLER_END_FRAME,
) -> DeclaredCandidate:
    return DeclaredCandidate(
        segment_id=segment_id,
        kind=kind,
        text=text,
        start_frame=start_frame,
        end_frame=end_frame,
        content_score=5,
        clarity_score=5,
        pause_ms=0,
    )


def _empty(
    segment_id: str,
    kind: Literal["pause", "filler", "false_start"] = "filler",
    start_frame: int = R6_SPEECH_START_FRAME,
    end_frame: int = R6_FILLER_END_FRAME,
) -> DeclaredCandidate:
    return _candidate(segment_id, kind, "", start_frame, end_frame)


def _speech(
    segment_id: str,
    text: str,
    start_frame: int = R6_SPEECH_START_FRAME,
    end_frame: int = R6_SPEECH_END_FRAME,
) -> DeclaredCandidate:
    return _candidate(segment_id, "speech", text, start_frame, end_frame)


def _transcript_row(
    label: str,
    text: str = "えーと",
    start_ms: int = R6_SEGMENT_START_MS,
    end_ms: int = R6_SEGMENT_END_MS,
) -> m.TranscriptHitRow:
    return m.TranscriptHitRow(
        source_id=AUDIO_SOURCE_ID,
        segment_index=0,
        span=m.MsSpan(start_ms=start_ms, end_ms=end_ms),
        text=text,
        analyzer_version="todo33-v1",
        artifact_sha=_sha(label),
    )


def _silence_row(label: str) -> m.SilenceHitRow:
    return m.SilenceHitRow(
        source_id=AUDIO_SOURCE_ID,
        kind="pause",
        span=m.SampleSpan(
            start_sample=R6_SPEECH_START_FRAME * 1600,
            end_sample=R6_FILLER_END_FRAME * 1600,
        ),
        sample_rate=48000,
        analyzer_version="todo34-v1",
        artifact_sha=_sha(label),
    )


def _methods(bundle: EvidenceBundle) -> list[tuple[str, str]]:
    return [(c.segment_id, c.method) for c in bundle.corroborations]


class _TranscriptIndexApi:
    """Hermetic MediaQueryApi stand-in with frozen page semantics."""

    def __init__(
        self,
        rows: Sequence[m.TranscriptHitRow] = (),
        silence_rows: Sequence[m.SilenceHitRow] = (),
    ) -> None:
        self._rows = tuple(rows)
        self._silence_rows = tuple(silence_rows)
        self.queries: list[str] = []

    def episode_summary(
        self, request: m.EpisodeSummaryRequest
    ) -> m.EpisodeSummaryResponse:
        row = m.EpisodeSummaryRow(
            source_id=request.source_id, sha256=_sha("episode-media"),
            source_kind="audio", sample_rate=48000, channels=1,
            analyzer_version="todo34-v1", artifact_sha=_sha("episode-artifact"),
        )
        return m.EpisodeSummaryResponse(total=1, rows=(row,))

    def silence_ranges(
        self, request: m.SilenceRangesRequest
    ) -> m.SilenceRangesResponse:
        matching = [
            row
            for row in self._silence_rows
            if row.span.start_sample < request.span.end_sample
            and row.span.end_sample > request.span.start_sample
        ]
        return m.SilenceRangesResponse(
            total=len(matching), rows=tuple(matching),
            limit=request.pagination.limit, offset=request.pagination.offset,
        )

    def search_transcripts(
        self, request: m.SearchTranscriptsRequest
    ) -> m.SearchTranscriptsResponse:
        if request.pagination.limit > m.FROZEN_MAX_PAGE_SIZE:
            raise AssertionError("frozen page size violated")
        self.queries.append(request.text_query)
        matching = [row for row in self._rows if request.text_query in row.text]
        page = matching[
            request.pagination.offset :
            request.pagination.offset + request.pagination.limit
        ]
        return m.SearchTranscriptsResponse(
            total=len(matching), rows=tuple(page),
            limit=request.pagination.limit, offset=request.pagination.offset,
        )


def _r6_request(candidates: tuple[DeclaredCandidate, ...]) -> DirectorRequest:
    base = manifest_request(load_manifest("p1-ref-02-pauses-fillers"))
    edit_source = base.edit_source.model_copy(update={"total_frames": 8468})
    return base.model_copy(update={"edit_source": edit_source, "candidates": candidates})


def _as_api(api: _TranscriptIndexApi) -> MediaQueryApi:
    return cast("MediaQueryApi", api)


def test_r6_empty_text_filler_inside_speech_is_corroborated() -> None:
    api = _TranscriptIndexApi((_transcript_row("r6-fi127"),))
    request = _r6_request((_speech("s99", "えーと"), _empty("fi127")))
    bundle = assemble_evidence(_as_api(api), request)
    assert _methods(bundle) == [
        ("s99", "transcript_search"),
        ("fi127", "transcript_containment"),
    ]
    assert api.queries == ["えーと", "えーと"]
    assert _sha("r6-fi127") in bundle.lineage


def test_empty_text_false_start_inside_speech_is_corroborated() -> None:
    api = _TranscriptIndexApi(
        (
            _transcript_row("r6-fi127"),
            _transcript_row("r6-fa128", "あのー", 166700, 171667),
        )
    )
    request = _r6_request(
        (
            _speech("s99", "えーと"),
            _empty("fi127"),
            _speech("s100", "あのー", 5001, 5150),
            _empty("fa128", "false_start", 5001, 5150),
        )
    )
    bundle = assemble_evidence(_as_api(api), request)
    assert _methods(bundle) == [
        ("s99", "transcript_search"),
        ("fi127", "transcript_containment"),
        ("s100", "transcript_search"),
        ("fa128", "transcript_containment"),
    ]


def test_empty_text_filler_outside_speech_coverage_stops_honestly() -> None:
    api = _TranscriptIndexApi((_transcript_row("r6-elsewhere", "はい", 9000, 12000),))
    request = _r6_request((_speech("s1", "はい", 300, 390), _empty("fi127")))
    with pytest.raises(EvidenceIncomplete, match="lies outside every text-bearing"):
        assemble_evidence(_as_api(api), request)


def test_filler_span_missing_from_transcript_rows_stops_honestly() -> None:
    api = _TranscriptIndexApi((_transcript_row("r6-short-row", end_ms=136500),))
    request = _r6_request((_speech("s99", "えーと"), _empty("fi127")))
    with pytest.raises(
        EvidenceIncomplete, match="not contained in any indexed transcript segment"
    ):
        assemble_evidence(_as_api(api), request)


def test_text_bearing_candidates_keep_transcript_search() -> None:
    api = _TranscriptIndexApi((_transcript_row("r6-text", "えー", end_ms=136000),))
    request = _r6_request((_speech("s99", "えー"), _candidate("fi127", "filler", "えー")))
    bundle = assemble_evidence(_as_api(api), request)
    assert _methods(bundle) == [
        ("s99", "transcript_search"),
        ("fi127", "transcript_search"),
    ]


def test_pause_candidates_keep_silence_overlap() -> None:
    api = _TranscriptIndexApi(silence_rows=(_silence_row("r6-pa49"),))
    request = _r6_request((_empty("pa49", "pause"),))
    bundle = assemble_evidence(_as_api(api), request)
    assert _methods(bundle) == [("pa49", "silence_overlap")]
    assert api.queries == []


def test_empty_text_speech_declaration_keeps_original_stop() -> None:
    request = _r6_request((_candidate("s99", "speech", ""),))
    with pytest.raises(
        EvidenceIncomplete, match="no searchable text and no silence-corroborable kind"
    ):
        assemble_evidence(_as_api(_TranscriptIndexApi()), request)


def test_budget_exceeded_during_containment_search_stops_typed() -> None:
    class _BudgetApi(_TranscriptIndexApi):
        def search_transcripts(
            self, request: m.SearchTranscriptsRequest
        ) -> m.SearchTranscriptsResponse:
            raise m.ApiBudgetExceeded(
                "page window exceeds the frozen budget of 500 rows"
            )

    request = _r6_request((_empty("fi127"), _speech("s99", "えーと")))
    with pytest.raises(EvidenceIncomplete, match="frozen budget"):
        assemble_evidence(_as_api(_BudgetApi()), request)


def test_r7_cover_shifted_by_exactly_lattice_is_corroborated() -> None:
    api = _TranscriptIndexApi((_transcript_row("r7-fi127"),))
    request = _r6_request(
        (
            _speech("s51", "えーと", R7_SHIFTED_SPEECH_START_FRAME, R6_SPEECH_END_FRAME),
            _empty("fi127"),
        )
    )
    bundle = assemble_evidence(_as_api(api), request)
    assert _methods(bundle) == [
        ("s51", "transcript_search"),
        ("fi127", "transcript_containment"),
    ]
    assert _sha("r7-fi127") in bundle.lineage


def test_r7_cover_shifted_beyond_lattice_stops_honestly() -> None:
    api = _TranscriptIndexApi((_transcript_row("r7-fi127"),))
    request = _r6_request(
        (
            _speech(
                "s51", "えーと",
                R7_SHIFTED_SPEECH_START_FRAME + 1, R6_SPEECH_END_FRAME,
            ),
            _empty("fi127"),
        )
    )
    with pytest.raises(EvidenceIncomplete, match="lies outside every text-bearing"):
        assemble_evidence(_as_api(api), request)


def test_r7_lattice_cover_found_but_index_misses_stops_honestly() -> None:
    api = _TranscriptIndexApi(
        (_transcript_row("r7-elsewhere", "えーと", 9000, 12000),)
    )
    request = _r6_request(
        (
            _speech("s51", "えーと", R7_SHIFTED_SPEECH_START_FRAME, R6_SPEECH_END_FRAME),
            _empty("fi127"),
        )
    )
    with pytest.raises(
        EvidenceIncomplete, match="not contained in any indexed transcript segment"
    ):
        assemble_evidence(_as_api(api), request)
