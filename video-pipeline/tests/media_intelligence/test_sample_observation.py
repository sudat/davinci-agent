"""GLM chunked sample observation: chunk math, validation, local pick (PRD v4.4 §8.5).

Pure-function tests with INJECTED observation fixtures — no GLM network.
The live scout call (``observe_chunk``) is excluded; the operator smoke-tests
it against the real episode.
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest
from pydantic import ValidationError

from services.compile.sample_projection import windows_around_anchors
from services.contracts.primitives import RecordFrameSpan
from services.media_intelligence.sample_observation import (
    SampleCandidate,
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    chunk_frame_range,
    observation_record,
    partition_chunks,
    pick_candidate_anchors,
    resolve_observed_windows,
    slice_transcript,
)
from services.media_intelligence.sample_observation_wire import (
    sample_observation_request_body,
    sample_observation_response_parser,
)
from services.media_intelligence.video_review_wire import (
    UNTRUSTED_DATA_MARKER,
    VideoProviderError,
)
from tests.compile.test_sample_projection import _full_ir


def _chunk(index: int, start: float, end: float) -> SampleChunk:
    return SampleChunk(index=index, start_seconds=start, end_seconds=end)


def _obs(
    index: int,
    start: float,
    end: float,
    reasons: tuple[str, ...] = (),
    *,
    insufficient: bool = False,
) -> SampleChunkObservation:
    return SampleChunkObservation(
        chunk_index=index,
        chunk_start_seconds=start,
        chunk_end_seconds=end,
        findings=("something visible",),
        candidates=tuple(
            SampleCandidate(
                start_second=start + 1.0, end_second=start + 3.0, reason=reason
            )
            for reason in reasons
        ),
        quality_insufficient=insufficient,
        insufficiency_note="probe-note" if insufficient else None,
    )


def test_partition_is_contiguous_without_overlap_or_gap() -> None:
    chunks = partition_chunks(150.0)
    assert [(c.start_seconds, c.end_seconds) for c in chunks] == [
        (0.0, 60.0),
        (60.0, 120.0),
        (120.0, 150.0),
    ]
    assert partition_chunks(120.0)[-1].end_seconds == 120.0
    assert len(partition_chunks(45.0)) == 1
    with pytest.raises(SampleObservationError):
        partition_chunks(0.0)


def test_chunk_frame_range_uses_exact_rational_math() -> None:
    assert chunk_frame_range(_chunk(0, 0.0, 60.0), Fraction(30, 1), 5400) == (0, 1800)
    assert chunk_frame_range(_chunk(2, 120.0, 150.0), Fraction(30, 1), 4500) == (
        3600,
        4500,
    )


def test_slice_transcript_keeps_overlaps_in_order() -> None:
    chunk = _chunk(1, 60.0, 120.0)
    segments = [
        (0.0, 10.0, "before"),
        (55.0, 65.0, "straddles start"),
        (90.0, 95.0, "inside"),
        (115.0, 130.0, "straddles end"),
        (200.0, 210.0, "after"),
    ]
    assert (
        slice_transcript(segments, chunk) == "straddles start inside straddles end"
    )
    assert slice_transcript(segments, _chunk(0, 10.0, 11.0)) == ""


def test_observation_validation_refuses_outside_stamps_and_empty_text() -> None:
    with pytest.raises(ValidationError):
        SampleChunkObservation(
            chunk_index=0,
            chunk_start_seconds=0.0,
            chunk_end_seconds=60.0,
            findings=("ok",),
            candidates=(
                SampleCandidate(start_second=70.0, end_second=72.0, reason="late"),
            ),
        )
    with pytest.raises(ValidationError):
        SampleChunkObservation(
            chunk_index=0,
            chunk_start_seconds=0.0,
            chunk_end_seconds=60.0,
            findings=(),
        )
    with pytest.raises(ValidationError):
        SampleChunkObservation(
            chunk_index=0,
            chunk_start_seconds=0.0,
            chunk_end_seconds=60.0,
            findings=("ok",),
            scene_changes=([50.0, 70.0],),
        )


def test_observation_accepts_json_int_seconds() -> None:
    observation = SampleChunkObservation.model_validate(
        {
            "chunk_index": 0,
            "chunk_start_seconds": 0,
            "chunk_end_seconds": 60,
            "findings": ["visible action"],
            "scene_changes": [[5, 9]],
            "candidates": [{"start_second": 10, "end_second": 12, "reason": "a laugh"}],
        }
    )
    assert observation.candidates[0].start_second == 10.0


def test_pick_spreads_across_chunks_dedupes_and_skips_insufficient() -> None:
    observations = (
        _obs(0, 0.0, 60.0, ("first laugh",)),
        _obs(1, 60.0, 120.0, ("first laugh", "a door slams")),
        _obs(2, 120.0, 180.0, ("a quiet look",), insufficient=True),
        _obs(3, 180.0, 240.0, ("crowd cheers",)),
    )
    anchors = pick_candidate_anchors(observations)
    assert anchors == (2.0, 62.0, 182.0)
    assert pick_candidate_anchors(()) == ()


def test_pick_fills_second_round_from_rich_chunks() -> None:
    observations = (_obs(0, 0.0, 60.0, ("one", "two", "three", "four")),)
    assert pick_candidate_anchors(observations) == (2.0, 2.0, 2.0)


def test_observation_record_lists_insufficient_chunks() -> None:
    chunks = partition_chunks(150.0)
    observations = (
        _obs(0, 0.0, 60.0, ("a laugh",)),
        _obs(1, 60.0, 120.0, insufficient=True),
    )
    record = observation_record(chunks, observations, (2.0,))
    assert record["insufficient_chunks"] == [1, 2]
    assert record["anchors_seconds"] == [2.0]
    unobserved = record["chunks"][2]
    assert unobserved["quality_insufficient"] is True


def test_resolve_maps_candidates_to_widened_record_windows() -> None:
    full_ir = _full_ir()
    observations = (
        _obs(0, 0.0, 60.0, ("opening line",)),
        _obs(1, 60.0, 120.0, ("a turn",)),
    )
    windows = resolve_observed_windows(
        full_ir=full_ir, source_rate=Fraction(30, 1), observations=observations
    )
    assert windows == windows_around_anchors(full_ir, [30, 90])
    assert windows[0] == RecordFrameSpan(start_frame=0, end_frame=120)


def test_resolve_with_no_candidates_is_typed_blocked() -> None:
    with pytest.raises(SampleObservationError) as exc_info:
        resolve_observed_windows(
            full_ir=_full_ir(),
            source_rate=Fraction(30, 1),
            observations=(_obs(0, 0.0, 60.0, insufficient=True),),
        )
    assert exc_info.value.code == "sample-observation-insufficient"


def test_wire_body_separates_untrusted_data_and_parser_pins_chunk() -> None:
    body = json.loads(sample_observation_request_body("glm-5v-turbo", "DATA", "eG1s"))
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["text"].startswith(UNTRUSTED_DATA_MARKER + "\nDATA")
    assert parts[2]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert body["thinking"] == {"type": "disabled"}

    chunk = _chunk(0, 0.0, 60.0)
    payload = {
        "chunk_index": 0,
        "chunk_start_seconds": 0.0,
        "chunk_end_seconds": 60.0,
        "findings": ["a visible thing"],
        "candidates": [{"start_second": 4.0, "end_second": 6.0, "reason": "why"}],
    }
    raw = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
    assert sample_observation_response_parser(chunk)(raw).candidates[0].reason == "why"

    wrong = dict(payload, chunk_index=7)
    bad = json.dumps({"choices": [{"message": {"content": json.dumps(wrong)}}]}).encode()
    with pytest.raises(VideoProviderError, match="provider-range-mismatch"):
        sample_observation_response_parser(chunk)(bad)
    with pytest.raises(VideoProviderError, match="provider-bad-response"):
        sample_observation_response_parser(chunk)(b"not json")
