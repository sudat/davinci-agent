"""IR checks: coverage gaps/overlaps, totals, links, Todo-44 reuse."""

from __future__ import annotations

from services.qc.checks import check_ir
from tests.qc.support import clean_policy, preset, rehash, timeline_ir

INPUTS = ("d" * 64,)


def rules(issues) -> set[str]:
    return {issue.rule_id for issue in issues}


def test_clean_ir_produces_no_issues() -> None:
    ir = timeline_ir(((0, 60), (60, 120)))
    assert check_ir(ir, clean_policy(preset()), INPUTS) == ()


def test_missing_binding_blocks_only_when_required() -> None:
    policy = clean_policy(preset())
    assert check_ir(None, policy, INPUTS) == ()
    required = rehash(
        policy.model_copy(
            update={"ir": policy.ir.model_copy(update={"require_binding": True})}
        )
    )
    issues = check_ir(None, required, INPUTS)
    assert rules(issues) == {"ir_binding_missing"}


def test_record_gap_blocks() -> None:
    ir = timeline_ir(((0, 60), (90, 120)))
    issues = check_ir(ir, clean_policy(preset()), INPUTS)
    assert "ir_span_gap" in rules(issues)
    gap = next(issue for issue in issues if issue.rule_id == "ir_span_gap")
    assert "[60,90)" in gap.detail


def test_totals_mismatch_between_tracks_blocks() -> None:
    ir = timeline_ir(((0, 60), (60, 120)), audio_spans=((0, 60), (60, 100)))
    issues = check_ir(ir, clean_policy(preset()), INPUTS)
    assert "ir_totals_mismatch" in rules(issues)


def test_declared_total_mismatch_blocks() -> None:
    policy = clean_policy(preset())
    with_total = rehash(
        policy.model_copy(
            update={"ir": policy.ir.model_copy(update={"expected_total_frames": 999})}
        )
    )
    ir = timeline_ir(((0, 60), (60, 120)))
    issues = check_ir(ir, with_total, INPUTS)
    assert "ir_totals_mismatch" in rules(issues)


def test_broken_av_link_blocks() -> None:
    ir = timeline_ir(((0, 60), (60, 120)), gap_audio=True)
    issues = check_ir(ir, clean_policy(preset()), INPUTS)
    assert "ir_link_mismatch" in rules(issues)


def test_todo44_subtitle_rules_reused_on_cues() -> None:
    long_text = "x" * 50
    ir = timeline_ir(
        ((0, 60), (60, 120)),
        cue_spans=((0, 20, "ok-cue"), (18, 26, long_text)),
    )
    issues = check_ir(ir, clean_policy(preset()), INPUTS)
    found = rules(issues)
    assert "subtitle_cue_overlap" in found
    assert "subtitle_max_chars" in found
