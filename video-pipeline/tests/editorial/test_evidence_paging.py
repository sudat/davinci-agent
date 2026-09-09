"""r5 regression: silence evidence lookup must page past the first frozen page.

Real footage (r5 ``ep-7ffb9d4ac7a3f5ed``) carries a 256-row silence index while
``FROZEN_MAX_PAGE_SIZE`` is 50; the old single-page fetch made candidate
``pa49``'s corroborating row ``[5114400,5181600)`` deterministically invisible
and typed-stopped before transport. These hermetic cases pin the paged
behavior through a fake that enforces the frozen page/window contract with
real overlap filtering and ``(kind, start, end)`` ordering.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

import pytest

from services.editorial.evidence import EvidenceIncomplete, assemble_evidence
from services.editorial.models import DeclaredCandidate, DirectorRequest
from services.media_query import api_models as m
from services.media_query.row_models import AUDIO_SOURCE_ID
from tests.editorial.support import load_manifest, manifest_request

if TYPE_CHECKING:
    from services.media_query.api import MediaQueryApi

R5_SILENCE_START = 5114400
R5_SILENCE_END = 5181600
R5_START_FRAME = 3198
R5_END_FRAME = 3240


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _silence_row(start: int, end: int, *, label: str) -> m.SilenceHitRow:
    return m.SilenceHitRow(
        source_id=AUDIO_SOURCE_ID,
        kind="pause",
        span=m.SampleSpan(start_sample=start, end_sample=end),
        sample_rate=48000,
        analyzer_version="todo34-v1",
        artifact_sha=_sha(label),
    )


class _PagedSilenceApi:
    """Hermetic MediaQueryApi stand-in with frozen paging semantics."""

    def __init__(
        self, rows: tuple[m.SilenceHitRow, ...], *, total_override: int | None = None
    ) -> None:
        ordered = sorted(rows, key=lambda row: (row.kind, row.span.start_sample))
        self._rows = ordered
        self._total_override = total_override
        self.offsets: list[int] = []

    def episode_summary(
        self, request: m.EpisodeSummaryRequest
    ) -> m.EpisodeSummaryResponse:
        row = m.EpisodeSummaryRow(
            source_id=request.source_id,
            sha256=_sha("episode-media"),
            source_kind="audio",
            sample_rate=48000,
            channels=1,
            analyzer_version="todo34-v1",
            artifact_sha=_sha("episode-artifact"),
        )
        return m.EpisodeSummaryResponse(total=1, rows=(row,))

    def silence_ranges(
        self, request: m.SilenceRangesRequest
    ) -> m.SilenceRangesResponse:
        if request.pagination.limit > m.FROZEN_MAX_PAGE_SIZE:
            raise AssertionError("frozen page size violated")
        window = request.pagination.offset + request.pagination.limit
        if window > m.FROZEN_ROW_BUDGET:
            raise m.ApiBudgetExceeded(
                f"page window exceeds the frozen budget of {m.FROZEN_ROW_BUDGET} rows"
            )
        matching = [
            row
            for row in self._rows
            if row.span.start_sample < request.span.end_sample
            and row.span.end_sample > request.span.start_sample
        ]
        total = self._total_override if self._total_override is not None else len(matching)
        page = matching[request.pagination.offset : request.pagination.offset
            + request.pagination.limit]
        self.offsets.append(request.pagination.offset)
        return m.SilenceRangesResponse(
            total=total,
            rows=tuple(page),
            limit=request.pagination.limit,
            offset=request.pagination.offset,
        )

    def search_transcripts(
        self, request: m.SearchTranscriptsRequest
    ) -> m.SearchTranscriptsResponse:
        raise AssertionError("pause-only requests never search transcripts")


def _pa49_request() -> DirectorRequest:
    base = manifest_request(load_manifest("p1-ref-02-pauses-fillers"))
    edit_source = base.edit_source.model_copy(update={"total_frames": 6000})
    candidate = DeclaredCandidate(
        segment_id="pa49",
        kind="pause",
        text="",
        start_frame=R5_START_FRAME,
        end_frame=R5_END_FRAME,
        content_score=0,
        clarity_score=0,
        pause_ms=1400,
    )
    return base.model_copy(update={"edit_source": edit_source, "candidates": (candidate,)})


def _decoys(count: int, start: int, step: int) -> list[m.SilenceHitRow]:
    return [
        _silence_row(start + index * step, start + index * step + 1000, label=f"decoy-{index}")
        for index in range(count)
    ]


def _as_api(api: _PagedSilenceApi) -> MediaQueryApi:
    return cast("MediaQueryApi", api)


def test_corroborating_row_past_first_page_is_found() -> None:
    decoys_before = _decoys(200, 1000, 20000)
    decoys_after = [
        _silence_row(5200000 + index * 50000, 5200000 + index * 50000 + 1000,
                     label=f"late-{index}")
        for index in range(55)
    ]
    corroborator = _silence_row(R5_SILENCE_START, R5_SILENCE_END, label="r5-pa49")
    api = _PagedSilenceApi((*decoys_before, *decoys_after, corroborator))
    bundle = assemble_evidence(_as_api(api), _pa49_request())
    assert [(c.segment_id, c.method) for c in bundle.corroborations] == [
        ("pa49", "silence_overlap")
    ]
    assert api.offsets == [0, 50, 100, 150, 200, 250]


def test_single_page_index_issues_one_request() -> None:
    decoys = _decoys(49, 1000, 20000)
    corroborator = _silence_row(R5_SILENCE_START, R5_SILENCE_END, label="r5-pa49-small")
    api = _PagedSilenceApi((*decoys, corroborator))
    bundle = assemble_evidence(_as_api(api), _pa49_request())
    assert [(c.segment_id, c.method) for c in bundle.corroborations] == [
        ("pa49", "silence_overlap")
    ]
    assert api.offsets == [0]


def test_genuinely_missing_row_still_stops_honestly() -> None:
    decoys_before = _decoys(200, 1000, 20000)
    decoys_after = [
        _silence_row(5200000 + index * 50000, 5200000 + index * 50000 + 1000,
                     label=f"late-{index}")
        for index in range(56)
    ]
    api = _PagedSilenceApi((*decoys_before, *decoys_after))
    with pytest.raises(EvidenceIncomplete, match="no overlapping silence evidence"):
        assemble_evidence(_as_api(api), _pa49_request())
    assert api.offsets == [0, 50, 100, 150, 200, 250]


def test_page_bound_exceeded_is_a_typed_honest_error() -> None:
    rows = tuple(
        _silence_row(1000 + index * 1000, 1000 + index * 1000 + 500, label=f"row-{index}")
        for index in range(500)
    )
    api = _PagedSilenceApi(rows, total_override=600)
    with pytest.raises(EvidenceIncomplete, match="10-page bound"):
        assemble_evidence(_as_api(api), _pa49_request())
    assert len(api.offsets) == 10
