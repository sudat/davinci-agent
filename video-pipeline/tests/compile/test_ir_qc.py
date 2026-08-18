"""Todo 44 acceptance: deterministic QC over the production Timeline IR.

Every rule violation carries a typed rule id; hand-crafted malformed IRs
(bypassing generator-level guarantees) must still be caught here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.compile.ir_qc import IrQcError, require_ir_qc, run_ir_qc
from services.contracts.primitives import RecordFrameSpan
from services.contracts.timeline_ir import TimelineTrackProduction
from services.foundation_io import canonical_model_bytes
from services.validate.edit_commit_schema import tuplize

from .support import compile_fixture, default_policy, golden_transcript_cue_source

REF01 = "p1-ref-01-clean-ja"
SUBTITLE_TRACK = 3


def _cues(result):
    return [
        item
        for track in result.ir.tracks
        if track.track.index == SUBTITLE_TRACK
        for item in track.items
        if item.kind == "subtitle_cue"
    ]


def _compiled_with_one_cue():
    return compile_fixture(
        REF01, transcript=golden_transcript_cue_source(REF01, ("s1",))
    )


def _rule_ids(excinfo: pytest.ExceptionInfo[IrQcError]) -> list[str]:
    return [violation.rule_id for violation in excinfo.value.violations]


def test_min_duration_violation_is_typed() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    shortened = cue.model_copy(update={"record_span": RecordFrameSpan(start_frame=0, end_frame=3)})
    tampered_track = result.ir.tracks[2].model_copy(update={"items": (shortened,)})
    tampered = result.ir.model_copy(update={"tracks": (*result.ir.tracks[:2], tampered_track)})

    with pytest.raises(IrQcError) as error:
        require_ir_qc(tampered, default_policy())
    assert "subtitle_min_duration" in _rule_ids(error)


def test_overlapping_cues_violation_is_typed() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    twin = cue.model_copy(update={"record_span": RecordFrameSpan(start_frame=100, end_frame=200)})
    tampered_track = result.ir.tracks[2].model_copy(update={"items": (cue, twin)})
    tampered = result.ir.model_copy(update={"tracks": (*result.ir.tracks[:2], tampered_track)})

    with pytest.raises(IrQcError) as error:
        require_ir_qc(tampered, default_policy())
    assert "subtitle_cue_overlap" in _rule_ids(error)


def test_max_lines_violation_is_typed() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    wordy = cue.model_copy(update={"lines": ("一行目。", "二行目。", "三行目。")})
    tampered_track = result.ir.tracks[2].model_copy(update={"items": (wordy,)})
    tampered = result.ir.model_copy(update={"tracks": (*result.ir.tracks[:2], tampered_track)})

    with pytest.raises(IrQcError) as error:
        require_ir_qc(tampered, default_policy())
    assert "subtitle_max_lines" in _rule_ids(error)


def test_max_chars_violation_is_typed() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    long_line = cue.model_copy(update={"lines": ("あ" * 25,)})
    tampered_track = result.ir.tracks[2].model_copy(update={"items": (long_line,)})
    tampered = result.ir.model_copy(update={"tracks": (*result.ir.tracks[:2], tampered_track)})

    with pytest.raises(IrQcError) as error:
        require_ir_qc(tampered, default_policy())
    assert "subtitle_max_chars" in _rule_ids(error)


def test_safe_area_violation_is_typed() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    unsafe = cue.model_copy(update={"safe_area": False})
    tampered_track = result.ir.tracks[2].model_copy(update={"items": (unsafe,)})
    tampered = result.ir.model_copy(update={"tracks": (*result.ir.tracks[:2], tampered_track)})

    with pytest.raises(IrQcError) as error:
        require_ir_qc(tampered, default_policy())
    assert "subtitle_safe_area" in _rule_ids(error)


def test_track_kind_correctness_is_enforced_at_model_level() -> None:
    result = _compiled_with_one_cue()
    cue = _cues(result)[0]
    video_track = result.ir.tracks[0]
    payload = video_track.model_dump(mode="json")
    payload["items"].append(cue.model_dump(mode="json"))

    with pytest.raises(ValidationError, match="track_kind_mismatch"):
        TimelineTrackProduction.model_validate(tuplize(payload))


def test_valid_ir_produces_no_violations() -> None:
    result = _compiled_with_one_cue()

    assert run_ir_qc(result.ir, default_policy()) == ()


def test_qc_scans_serialized_document_for_resolve_fields() -> None:
    """The serialized document path stays clean (model rejects; QC re-checks)."""

    result = _compiled_with_one_cue()
    document = canonical_model_bytes(result.ir).decode()

    assert "resolve" not in document.lower()
    assert run_ir_qc(result.ir, default_policy()) == ()
