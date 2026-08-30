"""Focused tests for explicit AudioFacts injection in finishing plans."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.cli._v44_finishing_plans import build_finishing_plans, load_audio_facts
from services.cli.v44_finishing import _parser
from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.creative_plan.audio_finishing import AudioFactsV1
from services.creative_plan.compile_ir_v2 import (
    SourceFactsV2,
    SourceFactV2,
    TranscriptFactV2,
    compile_ir_v2,
)
from services.creative_plan.edit_models_v2 import (
    CreativeEditPlanProposalV2,
)
from services.creative_plan.ir_models_v2 import SubtitleCueV2, TimelineIrV2
from services.production_kit.preview import KitDomainSelectionV1, KitSelectionRecordV1
from services.production_kit.registry import load_kit

RATE = RationalFrameRate(num=30, den=1)
EPISODE_ID = "ep-audio-facts-unit"
SOURCE_ID = "src-ep"


def _ir_with_audio_roles() -> TimelineIrV2:
    from services.editorial_v2.moment_models import (  # noqa: PLC0415
        MomentCandidateV2,
        MomentProvenance,
        MomentSelectionProposalV2,
        MomentSourceSpan,
    )

    def cand(cid: str, s: int, e: int) -> MomentCandidateV2:
        return MomentCandidateV2(
            candidate_id=cid,
            candidate_type="speech",
            source_span=MomentSourceSpan(start_frame=s, end_frame=e),
            intent="keep",
            rationale="x",
            evidence_refs=(f"shot-{cid}",),
            confidence=0.8,
            provenance=MomentProvenance(producer="test", version="1"),
        )

    selection = MomentSelectionProposalV2(
        proposal_id="msel-unit",
        episode_id=EPISODE_ID,
        candidates=(cand("c1", 0, 90),),
    )
    ops = [{"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "c1"}]
    plan31 = CreativeEditPlanProposalV2.model_validate(
        {
            "schema_version": "creative-edit-plan-v2",
            "proposal_id": "cep-unit",
            "episode_id": EPISODE_ID,
            "selection_ref": {"proposal_id": "msel-unit"},
            "operations": ops,
        }
    )
    facts = SourceFactsV2(
        rate=RATE,
        sources=(SourceFactV2(source_id=SOURCE_ID, duration_frames=90),),
        transcripts=(
            TranscriptFactV2(
                segment_id="tr-a1",
                source_id=SOURCE_ID,
                text="hello",
                start_frame=0,
                end_frame=90,
            ),
        ),
    )
    ir = compile_ir_v2(selection, plan31, source_facts=facts)
    if not ir.subtitle_cues:
        cue = SubtitleCueV2(
            cue_id="cue-1",
            text="hello world",
            record_span=RecordFrameSpan(start_frame=0, end_frame=30),
            candidate_ref="c1",
            transcript_ref="tr-a1",
        )
        ir = ir.model_copy(update={"subtitle_cues": (cue,)})
    return ir


def _kit_and_record(tmp_path: Path):
    kit = load_kit()
    record = KitSelectionRecordV1(
        episode_id=EPISODE_ID,
        entries=(
            KitDomainSelectionV1(
                domain="subtitle",
                recipe_id="subtitle/default",
                semantic_intent="subtitle_track",
                recorded_at="2026-08-23T00:00:00Z",
            ),
            KitDomainSelectionV1(
                domain="audio",
                recipe_id="audio/dialogue-chain",
                semantic_intent="voice_isolation",
                recorded_at="2026-08-23T00:00:00Z",
            ),
            KitDomainSelectionV1(
                domain="color",
                recipe_id="color/channel-look",
                semantic_intent="color_look",
                recorded_at="2026-08-23T00:00:00Z",
            ),
        ),
    )
    episode_root = tmp_path / EPISODE_ID
    episode_root.mkdir(parents=True, exist_ok=True)
    return kit, record, episode_root


def test_explicit_audio_facts_disable_cleanup_and_normalization(tmp_path: Path) -> None:
    ir = _ir_with_audio_roles()
    kit, record, episode_root = _kit_and_record(tmp_path)
    facts = AudioFactsV1(
        episode_id=EPISODE_ID,
        dialogue_clean=True,
        has_bgm=False,
        has_ambience=False,
        measured_loudness_ok=True,
    )
    plans = build_finishing_plans(
        ir, kit=kit, record=record, episode_root=episode_root, audio_facts=facts
    )
    assert plans.audio_plan is not None
    stages = {s.stage: s for s in plans.audio_plan.stages}
    assert stages["dialogue_cleanup"].enabled is False
    assert "already-good" in (stages["dialogue_cleanup"].justification or "")
    assert stages["dialogue_level_normalization"].enabled is False
    assert "already-good" in (stages["dialogue_level_normalization"].justification or "")


def test_absent_audio_facts_keeps_cleanup_enabled(tmp_path: Path) -> None:
    ir = _ir_with_audio_roles()
    kit, record, episode_root = _kit_and_record(tmp_path)
    plans = build_finishing_plans(
        ir, kit=kit, record=record, episode_root=episode_root, audio_facts=None
    )
    assert plans.audio_plan is not None
    stages = {s.stage: s for s in plans.audio_plan.stages}
    assert stages["dialogue_cleanup"].enabled is True
    assert stages["dialogue_level_normalization"].enabled is True


def test_audio_facts_episode_mismatch_raises(tmp_path: Path) -> None:
    ir = _ir_with_audio_roles()
    kit, record, episode_root = _kit_and_record(tmp_path)
    facts = AudioFactsV1(
        episode_id="ep-other",
        dialogue_clean=True,
        has_bgm=False,
        has_ambience=False,
        measured_loudness_ok=True,
    )
    with pytest.raises(FinishingError) as exc:
        build_finishing_plans(
            ir, kit=kit, record=record, episode_root=episode_root, audio_facts=facts
        )
    assert exc.value.code == "audio-facts-episode-mismatch"


# Production CLI route: without --audio-facts the conservative default
# (dialogue_clean=False) makes a clean-dialogue episode mechanically
# unfinishable through the production route (defect; rerun8 evidence).


def test_run_parser_accepts_audio_facts_flag(tmp_path: Path) -> None:
    facts = tmp_path / "audio-facts.json"
    facts.write_bytes(b"{}")
    args = _parser().parse_args(
        [
            "run",
            "--episode-root", str(tmp_path),
            "--executor", "fake",
            "--audio-facts", str(facts),
        ]
    )
    assert args.audio_facts == facts


def test_load_audio_facts_none_passes_through() -> None:
    assert load_audio_facts(None) is None


def test_load_audio_facts_valid_file(tmp_path: Path) -> None:
    facts = tmp_path / "audio-facts.json"
    facts.write_bytes(
        AudioFactsV1(
            episode_id=EPISODE_ID,
            dialogue_clean=True,
            has_bgm=False,
            has_ambience=False,
            measured_loudness_ok=False,
        ).model_dump_json().encode()
    )
    loaded = load_audio_facts(facts)
    assert loaded is not None
    assert loaded.dialogue_clean is True
    assert loaded.measured_loudness_ok is False


def test_load_audio_facts_invalid_file_is_typed_error(tmp_path: Path) -> None:
    bad = tmp_path / "audio-facts.json"
    bad.write_bytes(b'{"episode_id": "x", "dialogue_clean": "yes"}')
    with pytest.raises(FinishingMalformedError) as exc:
        load_audio_facts(bad)
    assert exc.value.code == "audio-facts-invalid"

    missing = tmp_path / "absent.json"
    with pytest.raises(FinishingMalformedError) as exc:
        load_audio_facts(missing)
    assert exc.value.code == "audio-facts-invalid"
