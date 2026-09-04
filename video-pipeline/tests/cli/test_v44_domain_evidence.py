"""§12.2 domain-judgment derivation tests (deterministic slice 1).

Retrospective oracle: v44-real-01 graphics_presentation was recorded
intentionally_not_needed ("no request") — the 2026-09-01 diagnosis and the
accepted 2026-09-03 deliverable prove telop WAS needed. The new derivation
must propose it with episode evidence. Conform checks read the ACTUAL
normalize-record content; the operator not-needed path is the minimal
runtime record decided in this slice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.cli._v44_domain_evidence import (
    DomainEvidenceError,
    conform_scale_status,
    derive_domain_facts,
    load_episode_domain_evidence,
)
from services.cli.v44_finishing import _parser as _finishing_parser
from services.creative_plan.quality_domains import (
    ExecutionFactsV1,
    QualityDomainEntryV1,
    QualityPlansV1,
    build_domain_report,
    evaluate_quality_gate,
)

REAL_EPISODE = Path(
    "/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01"
    "/runs/cockpit-v44-1/episodes/ep-457dfac97989568e"
)


def _synthetic_episode(tmp_path: Path, *, protocol: dict | None,
                        chapter: dict | None, decisions: dict | None,
                        normalize: dict | None) -> Path:
    root = tmp_path / "ep"
    (root / "runtime").mkdir(parents=True)
    (root / "run").mkdir(parents=True)
    if protocol is not None:
        (root / "episode.json").write_text(json.dumps(protocol, ensure_ascii=False))
    if chapter is not None:
        (root / "runtime" / "chapter-title-proposal.json").write_text(
            json.dumps(chapter, ensure_ascii=False))
    if decisions is not None:
        (root / "runtime" / "domain-decisions.json").write_text(
            json.dumps(decisions, ensure_ascii=False))
    if normalize is not None:
        (root / "run" / "normalize-record.json").write_text(
            json.dumps(normalize, ensure_ascii=False))
    return root


PROTOCOL_WITH_TITLE = {
    "schema_version": "v44-episode-protocol-v1",
    "episode_id": "ep-test",
    "created_at": "2026-09-03T00:00:00Z",
    "title_intent": "【テスト】タイトル【やばい】",
}

CHAPTER_PROPOSAL = {
    "approval_status": "pending",
    "episode_id": "ep-test",
    "boundary": {"left_item_id": "s20", "record_frame": 1632,
                 "right_item_id": "s21", "source_frame": 1845},
    "candidates": [{"candidate_id": "c1"}],
}


# ------------------------------------------------- conform content checks


def test_conform_scale_fit_performed_only_on_content_proof() -> None:
    fit = conform_scale_status(
        filters="fps=30,scale=1920:1080", mezzanine=(1920, 1080), timeline=(1920, 1080))
    no_scale = conform_scale_status(
        filters="fps=30,setpts=N/(30*TB)", mezzanine=(3840, 2160), timeline=(1920, 1080))
    crop = conform_scale_status(
        filters="fps=30,crop=1920:1080:960:540", mezzanine=(1920, 1080), timeline=(1920, 1080))
    absent = conform_scale_status(filters=None, mezzanine=(3840, 2160), timeline=(1920, 1080))
    matched = conform_scale_status(
        filters="fps=30", mezzanine=(1920, 1080), timeline=(1920, 1080))
    assert fit == "fit_performed"
    assert no_scale == "fit_not_performed"
    assert crop == "fit_not_performed"
    assert absent == "fit_not_performed"
    assert matched == "no_geometry_mismatch"


# ------------------------------------------- evidence-free regression


def test_evidence_free_episode_is_not_needed_with_evidence_citing_absence(
    tmp_path: Path,
) -> None:
    root = _synthetic_episode(
        tmp_path, protocol={
            "schema_version": "v44-episode-protocol-v1", "episode_id": "ep-test",
            "created_at": "2026-09-03T00:00:00Z", "title_intent": None},
        chapter=None, decisions=None,
        normalize={"argv": ["ffmpeg", "-vf", "fps=30"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=0, mezzanine_geometry=(1920, 1080))
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=0,
        run_report=None, qc_verdict_passed=False)
    assert facts.graphics_intended is False
    assert facts.framing_motion_intended is False

    report = build_domain_report(
        facts,
        plans=_plans(episode_id="ep-test"),
        execution_report=_execution("ep-test"),
    )
    graphics = report.domain_entry("graphics_presentation")
    framing = report.domain_entry("framing_motion")
    assert graphics.status == "intentionally_not_needed"
    assert "no title_intent" in (graphics.justification or "")
    assert "no graphic/still items" in (graphics.justification or "")
    assert "requested" not in (graphics.justification or "")
    assert framing.status == "intentionally_not_needed"
    assert "geometry matches" in (framing.justification or "")


# --------------------------------------------- retrospective oracle


@pytest.mark.skipif(not REAL_EPISODE.is_dir(), reason="private v44-real-01 episode absent")
def test_v44_real_01_graphics_and_framing_are_proposed_with_evidence() -> None:
    evidence = load_episode_domain_evidence(
        REAL_EPISODE, ir_graphics_items=0, mezzanine_geometry=(3840, 2160),
        protocol_path=REAL_EPISODE.parents[3] / "episode.json")
    facts = derive_domain_facts(
        evidence, episode_id="ep-457dfac97989568e", head_version=3, cue_count=100,
        run_report=None, qc_verdict_passed=False)

    assert facts.graphics_intended is True
    joined = " | ".join(facts.graphics_evidence)
    assert "title_intent" in joined
    assert "chapter-title-proposal" in joined
    assert "subtitle cues" in joined
    assert facts.framing_motion_intended is True
    framing_joined = " | ".join(facts.framing_motion_evidence)
    assert "normalize-record" in framing_joined
    assert "3840x2160" in framing_joined
    assert "1920x1080" in framing_joined

    report = build_domain_report(
        facts, plans=_plans(episode_id="ep-457dfac97989568e"),
        execution_report=_execution("ep-457dfac97989568e"))
    graphics = report.domain_entry("graphics_presentation")
    framing = report.domain_entry("framing_motion")
    assert graphics.status == "blocked"
    assert graphics.proposed is True
    assert any("title_intent" in line for line in graphics.proposal_basis)
    assert framing.status == "blocked"
    assert framing.proposed is True
    gate = evaluate_quality_gate(report)
    assert gate.decision == "reject"
    assert "graphics_presentation" in gate.blocked_domains
    assert "framing_motion" in gate.blocked_domains


# --------------------------------------------- operator not-needed (Q2)


def test_operator_not_needed_decision_overrides_evidence_of_need(
    tmp_path: Path,
) -> None:
    root = _synthetic_episode(
        tmp_path, protocol=PROTOCOL_WITH_TITLE, chapter=CHAPTER_PROPOSAL,
        decisions={
            "schema_version": "v44-domain-decisions-v1",
            "episode_id": "ep-test",
            "decisions": [{
                "domain": "graphics_presentation",
                "decision": "not_needed",
                "justification": "suda判定: 本エピソードはテロップなしで完結する",
                "decided_by": "operator",
            }],
        },
        normalize={"argv": ["ffmpeg", "-vf", "fps=30,scale=1920:1080"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=0, mezzanine_geometry=(1920, 1080))
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=5,
        run_report=None, qc_verdict_passed=False)
    assert facts.graphics_intended is True

    report = build_domain_report(
        facts, plans=_plans(episode_id="ep-test"), execution_report=_execution("ep-test"))
    graphics = report.domain_entry("graphics_presentation")
    assert graphics.status == "intentionally_not_needed"
    assert "本エピソードはテロップなしで完結する" in (graphics.justification or "")
    assert graphics.proposed is False


# --------------------------------------------- conform record both ways


def test_fit_conform_record_makes_framing_committed_not_proposed(
    tmp_path: Path,
) -> None:
    root = _synthetic_episode(
        tmp_path, protocol=PROTOCOL_WITH_TITLE, chapter=CHAPTER_PROPOSAL,
        decisions=None,
        normalize={"argv": ["ffmpeg", "-i", "in.MP4", "-vf", "fps=30,scale=1920:1080"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=0, mezzanine_geometry=(1920, 1080))
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=5,
        run_report=None, qc_verdict_passed=False)
    report = build_domain_report(
        facts, plans=_plans(episode_id="ep-test"), execution_report=_execution("ep-test"))
    framing = report.domain_entry("framing_motion")
    assert framing.status == "applied"
    assert framing.proposed is False
    assert any("scale" in line for line in framing.evidence_refs)


def test_center_crop_conform_record_keeps_framing_blocked_proposed(
    tmp_path: Path,
) -> None:
    root = _synthetic_episode(
        tmp_path, protocol=PROTOCOL_WITH_TITLE, chapter=CHAPTER_PROPOSAL,
        decisions=None,
        normalize={"argv": ["ffmpeg", "-i", "in.MP4", "-vf", "fps=30,crop=1920:1080"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=0, mezzanine_geometry=(1920, 1080))
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=5,
        run_report=None, qc_verdict_passed=False)
    report = build_domain_report(
        facts, plans=_plans(episode_id="ep-test"), execution_report=_execution("ep-test"))
    framing = report.domain_entry("framing_motion")
    assert framing.status == "blocked"
    assert framing.proposed is True


def test_intake_shaped_episode_json_is_not_protocol_evidence(tmp_path: Path) -> None:
    root = _synthetic_episode(
        tmp_path,
        protocol={"schema_version": "v44-episode-intake-v1", "episode_id": "ep-test",
                  "video_path": "/x.mp4"},
        chapter=None, decisions=None, normalize={"argv": ["ffmpeg", "-vf", "fps=30"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=0, mezzanine_geometry=(1920, 1080))
    assert evidence.title_intent is None
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=4,
        run_report=None, qc_verdict_passed=False)
    assert facts.graphics_intended is False
    assert "no title_intent" in "; ".join(facts.graphics_evidence)
    assert "committed subtitle cues: 4" in "; ".join(facts.graphics_evidence)


def test_ir_graphics_items_count_as_committed_evidence(tmp_path: Path) -> None:
    root = _synthetic_episode(
        tmp_path, protocol=PROTOCOL_WITH_TITLE, chapter=None, decisions=None,
        normalize={"argv": ["ffmpeg", "-vf", "fps=30,scale=1920:1080"]})
    evidence = load_episode_domain_evidence(
        root, ir_graphics_items=2, mezzanine_geometry=(1920, 1080))
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=5,
        run_report=None, qc_verdict_passed=False)
    report = build_domain_report(
        facts, plans=_plans(episode_id="ep-test"), execution_report=_execution("ep-test"))
    graphics = report.domain_entry("graphics_presentation")
    assert graphics.status == "applied"
    assert any("2 graphic/still items" in line for line in graphics.evidence_refs)


# ----------------------------------------------- additive model fields


def test_entry_new_fields_default_and_roundtrip() -> None:
    old_shape = QualityDomainEntryV1(
        domain="subtitle", status="intentionally_not_needed",
        justification="dialogue-free episode")
    assert old_shape.proposed is False
    assert old_shape.proposal_basis == ()
    roundtripped = QualityDomainEntryV1.model_validate_json(
        QualityDomainEntryV1(
            domain="graphics_presentation", status="blocked",
            justification="no committed plan", proposed=True,
            proposal_basis=("episode-protocol: title_intent present",),
        ).model_dump_json())
    assert roundtripped.proposed is True
    assert roundtripped.proposal_basis == ("episode-protocol: title_intent present",)


# --------------------------------------------------------------- helpers


def _plans(episode_id: str) -> QualityPlansV1:
    return QualityPlansV1()


def _execution(episode_id: str) -> ExecutionFactsV1:
    return ExecutionFactsV1(episode_id=episode_id, domains=())


# --------------------------------- explicit protocol path (option B)


def test_explicit_protocol_path_supplies_title_intent(tmp_path: Path) -> None:
    root = _synthetic_episode(
        tmp_path, protocol={
            "schema_version": "v44-episode-chain-manifest-v1", "episode_id": "ep-test",
            "video_path": "edit-source.mov"},
        chapter=None, decisions=None,
        normalize={"argv": ["ffmpeg", "-vf", "fps=30"]})
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({
        "schema_version": "v44-episode-protocol-v1", "episode_id": "ep-test",
        "created_at": "2026-09-03T00:00:00Z",
        "title_intent": "【テスト】デモタイトル【やばい】"}, ensure_ascii=False))
    default = load_episode_domain_evidence(root, mezzanine_geometry=(1920, 1080))
    assert default.title_intent is None
    evidence = load_episode_domain_evidence(
        root, protocol_path=protocol, mezzanine_geometry=(1920, 1080))
    assert evidence.title_intent == "【テスト】デモタイトル【やばい】"
    facts = derive_domain_facts(
        evidence, episode_id="ep-test", head_version=1, cue_count=3,
        run_report=None, qc_verdict_passed=False)
    assert facts.graphics_intended is True
    assert "title_intent present" in "; ".join(facts.graphics_evidence)


def test_explicit_protocol_path_malformed_is_typed_refusal(tmp_path: Path) -> None:
    root = _synthetic_episode(
        tmp_path, protocol=None, chapter=None, decisions=None, normalize=None)
    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps(
        {"schema_version": "v44-episode-chain-manifest-v1"}))
    with pytest.raises(DomainEvidenceError, match="protocol-schema"):
        load_episode_domain_evidence(root, protocol_path=wrong_schema)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json")
    with pytest.raises(DomainEvidenceError, match="protocol-invalid"):
        load_episode_domain_evidence(root, protocol_path=bad_json)
    with pytest.raises(DomainEvidenceError, match="protocol-missing"):
        load_episode_domain_evidence(root, protocol_path=tmp_path / "absent.json")


def test_run_cli_accepts_episode_protocol_flag(tmp_path: Path) -> None:
    args = _finishing_parser().parse_args([
        "run", "--episode-root", str(tmp_path),
        "--episode-protocol", str(tmp_path / "protocol.json")])
    assert args.episode_protocol == tmp_path / "protocol.json"
    default_args = _finishing_parser().parse_args(["run", "--episode-root", str(tmp_path)])
    assert default_args.episode_protocol is None
