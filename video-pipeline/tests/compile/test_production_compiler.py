"""Todo 44 acceptance: production Timeline IR compiler over the 42→43 chain.

Every compile runs the frozen five-fixture Edit Plans (Todo-42 solve + Todo-43
generate, from frozen manifests) through the production compiler and compares
RECOMPUTED placements against the frozen golden ``ir_records`` tables — never
against the compiler's own output (misleading-success guard). Failures are
typed errors with explicit codes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.compile.conform_inputs import EditSourceGeometry
from services.compile.ir_qc import run_ir_qc
from services.compile.production_errors import CompileProductionError
from services.compile.record_placement import AvPlacement, allocate_track
from services.compile.subtitle_policy import (
    FrameCueSpan,
    SampleCueSpan,
    TranscriptCueSegment,
    TranscriptCueSource,
)
from services.contracts.primitives import RationalFrameRate
from services.contracts.timeline_ir import TimelineIrProduction
from services.foundation_io import canonical_model_bytes
from tests.plan.support import ALL_FIXTURE_IDS

from .support import (
    compile_fixture,
    default_policy,
    geometry_for,
    golden_fixture,
)

if TYPE_CHECKING:
    from services.compile.production_compiler import CompileProductionResult

REF01 = "p1-ref-01-clean-ja"
REF02 = "p1-ref-02-pauses-fillers"
REF05 = "p1-ref-05-review-mix"

VIDEO_TRACK = 1
AUDIO_TRACK = 2
SUBTITLE_TRACK = 3


def _record_rows(result: CompileProductionResult) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for track in result.ir.tracks:
        for item in track.items:
            if item.kind == "gap":
                continue
            kind = "subtitle" if item.kind == "subtitle_cue" else item.kind
            text = item.text if item.kind == "subtitle_cue" else item.subtitle_text
            rows.append(
                (
                    kind,
                    track.track.index,
                    item.record_span.start_frame,
                    item.record_span.end_frame,
                    item.source.span.start_frame,
                    item.source.span.end_frame,
                    text,
                )
            )
    return rows


def _golden_rows(fixture_id: str) -> list[tuple[object, ...]]:
    records = golden_fixture(fixture_id)["ir_records"]
    assert isinstance(records, list)
    rows: list[tuple[object, ...]] = []
    for row in records:
        assert isinstance(row, dict)
        rows.append(
            (
                row["kind"],
                row["track_index"],
                row["record_start"],
                row["record_end"],
                row["source_start"],
                row["source_end"],
                row["subtitle_text"],
            )
        )
    return rows


# ------------------------------------------------------------------ goldens


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_five_fixture_plans_match_golden_record_tables(fixture_id: str) -> None:
    result = compile_fixture(fixture_id)

    assert _record_rows(result) == _golden_rows(fixture_id)
    assert result.ir.rate == RationalFrameRate(num=30, den=1)
    assert [t.track.index for t in result.ir.tracks] == sorted(
        t.track.index for t in result.ir.tracks
    )


def test_fixture_05_declared_subtitles_compile_to_exact_golden_cues() -> None:
    result = compile_fixture(REF05)

    subtitle_rows = [row for row in _record_rows(result) if row[1] == SUBTITLE_TRACK]
    assert subtitle_rows == [row for row in _golden_rows(REF05) if row[1] == SUBTITLE_TRACK]
    assert [row[6] for row in subtitle_rows] == [
        "このレストランは美味しいですね。",
        "看板メニューを二つ注文しました。",
        "はい、そうです。",
        "はい、そうです。",
    ]


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_compile_is_deterministic_and_rebuilds_identical_bytes(fixture_id: str) -> None:
    first = compile_fixture(fixture_id)
    second = compile_fixture(fixture_id)

    assert canonical_model_bytes(first.ir) == canonical_model_bytes(second.ir)
    assert first.ir.content_hash == second.ir.content_hash
    assert first.dropped_cues == second.dropped_cues


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_compiled_ir_passes_the_full_qc_rule_set(fixture_id: str) -> None:
    result = compile_fixture(fixture_id)

    assert run_ir_qc(result.ir, default_policy()) == ()


# -------------------------------------------------------- anchor resolution


def test_unresolved_anchor_is_a_typed_error() -> None:
    short = EditSourceGeometry(
        source_id=geometry_for(REF01).source_id,
        frame_rate=RationalFrameRate(num=30, den=1),
        total_frames=300,
        audio_sample_rate=48000,
    )

    with pytest.raises(CompileProductionError) as error:
        compile_fixture(REF01, geometry=short)
    assert error.value.code == "unresolved_anchor"


def test_unresolved_anchor_for_transcript_span_beyond_extent() -> None:
    runaway = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="範囲外",
                span=FrameCueSpan(start_frame=0, end_frame=9_999),
            ),
        ),
    )

    with pytest.raises(CompileProductionError, match="unresolved_anchor"):
        compile_fixture(REF01, transcript=runaway)


def test_frame_rate_mismatch_is_an_unresolved_anchor() -> None:
    wrong_rate = EditSourceGeometry(
        source_id=geometry_for(REF01).source_id,
        frame_rate=RationalFrameRate(num=24, den=1),
        total_frames=600,
        audio_sample_rate=48000,
    )

    with pytest.raises(CompileProductionError) as error:
        compile_fixture(REF01, geometry=wrong_rate)
    assert error.value.code == "unresolved_anchor"


# ------------------------------------------------- exact conform conversions


def test_sample_rate_mismatch_is_a_typed_error() -> None:
    foreign_rate = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="四万八千ではありません",
                span=SampleCueSpan(start_sample=0, end_sample=1000, sample_rate=44100),
            ),
        ),
    )

    with pytest.raises(CompileProductionError) as error:
        compile_fixture(REF01, transcript=foreign_rate)
    assert error.value.code == "sample_rate_mismatch"


def test_lossy_sample_conversion_is_refused() -> None:
    off_lattice = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="ずれています",
                span=SampleCueSpan(start_sample=1601, end_sample=240_000, sample_rate=48000),
            ),
        ),
    )

    with pytest.raises(CompileProductionError) as error:
        compile_fixture(REF01, transcript=off_lattice)
    assert error.value.code == "sample_conversion_lossy"


def test_exact_sample_span_compiles_identically_to_frame_span() -> None:
    in_samples = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="新幹線から見えた富士山です。",
                span=SampleCueSpan(start_sample=0, end_sample=240_000, sample_rate=48000),
            ),
        ),
    )
    in_frames = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="新幹線から見えた富士山です。",
                span=FrameCueSpan(start_frame=0, end_frame=150),
            ),
        ),
    )

    via_samples = compile_fixture(REF01, transcript=in_samples)
    via_frames = compile_fixture(REF01, transcript=in_frames)

    assert _record_rows(via_samples) == _record_rows(via_frames)


# ------------------------------------------------------ gaps and overlaps


def test_overlapping_cues_on_one_track_error() -> None:
    overlapping = TranscriptCueSource(
        language="ja",
        segments=(
            TranscriptCueSegment(
                segment_id="s1",
                text="重なっています",
                span=FrameCueSpan(start_frame=0, end_frame=200),
            ),
            TranscriptCueSegment(
                segment_id="s2",
                text="こちらも重なっています",
                span=FrameCueSpan(start_frame=100, end_frame=300),
            ),
        ),
    )

    with pytest.raises(CompileProductionError) as error:
        compile_fixture(REF01, transcript=overlapping)
    assert error.value.code == "record_overlap"


def test_explicit_record_gaps_are_legal_and_represented() -> None:
    items = (
        AvPlacement(
            item_id="v1",
            track_kind="video",
            source_id="src",
            source_start=0,
            source_end=100,
            record_start=0,
            record_end=100,
            av_link_id="v1",
        ),
        AvPlacement(
            item_id="v2",
            track_kind="video",
            source_id="src",
            source_start=200,
            source_end=300,
            record_start=150,
            record_end=250,
            av_link_id="v2",
        ),
    )

    allocation = allocate_track("video", items)

    assert [(i.record_start, i.record_end) for i in allocation.items] == [
        (0, 100),
        (150, 250),
    ]
    assert allocation.gaps == ((100, 150),)


def test_overlapping_placements_on_one_track_error() -> None:
    items = (
        AvPlacement(
            item_id="v1",
            track_kind="video",
            source_id="src",
            source_start=0,
            source_end=100,
            record_start=0,
            record_end=100,
            av_link_id="v1",
        ),
        AvPlacement(
            item_id="v2",
            track_kind="video",
            source_id="src",
            source_start=100,
            source_end=200,
            record_start=50,
            record_end=150,
            av_link_id="v2",
        ),
    )

    with pytest.raises(CompileProductionError, match="record_overlap"):
        allocate_track("video", items)


# -------------------------------------------------------- Resolve exclusion


def test_resolve_field_injected_into_production_ir_is_rejected() -> None:
    result = compile_fixture(REF01)
    payload = json.loads(canonical_model_bytes(result.ir))
    payload["tracks"][0]["items"][0]["resolve_clip_color"] = "blue"

    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        TimelineIrProduction.model_validate(payload)


def test_float_span_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TranscriptCueSource.model_validate(
            {
                "language": "ja",
                "segments": [
                    {
                        "segment_id": "s1",
                        "text": "浮動小数点",
                        "span": {"start_frame": 0.5, "end_frame": 10},
                    }
                ],
            }
        )
