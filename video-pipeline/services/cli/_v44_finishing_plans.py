"""Finishing-plan assembly for the T13 harness (task-11 selections → plans).

Subtitle/audio/color plans derive from the committed IR v2 + the operator's
recorded kit selections. A domain whose latest choice is ``none``
(どちらも不要/現状維持) gets NO plan — the existing plan-driven blocked
semantics then stand (quality_domains: no-plan ⇒ blocked with the fixed
justification). The subtitle plan is cue-driven (dialogue contract), not
selection-driven.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

from services.cli._v44_finishing_build import (
    MATRIX_PATH,
    PROPER_NOUNS_NAME,
    FinishingError,
    FinishingMalformedError,
    FinishingPlans,
)
from services.cli._v44_finishing_ir import asr_segments_from_cues
from services.cli._v44_subtitle_build import merged_proper_nouns
from services.creative_plan.audio_finishing import (
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    AudioFinishingPlanV1,
    AudioOpRequestV1,
    build_audio_plan,
)
from services.creative_plan.color_finishing import (
    ColorFactsV1,
    ColorFinishingPlanV1,
    ColorIssueV1,
    ColorPlanPolicy,
    McpCapabilityStatus,
    build_color_plan,
)
from services.creative_plan.compile_ir_v2 import SourceFactsV2, SourceFactV2
from services.creative_plan.subtitle_models import SubtitleBuildOptions
from services.creative_plan.subtitle_plan import build_subtitle_plan
from services.production_kit.preview import (
    KitPreviewError,
    KitSelectionRecordV1,
    latest_selections,
    recipe_selection_from_record,
)
from services.toolchain.mcp_fit import load_mcp_fit

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.production_kit.models import ChannelProductionKitV1
    from services.production_kit.recipe_select import RecipeSelection


def build_finishing_plans(
    ir_v2: TimelineIrV2,
    *,
    kit: ChannelProductionKitV1,
    record: KitSelectionRecordV1,
    episode_root: Path,
) -> FinishingPlans:
    if not ir_v2.subtitle_cues:
        raise FinishingError(
            "dialogue-missing",
            "the committed IR carries no subtitle cues; the v44-real-01 contract "
            "is a speech episode (finish a dialogue episode first)",
        )
    source_id = ir_v2.video_tracks[0].items[0].source.source_id
    segments, total = asr_segments_from_cues(ir_v2, source_id)
    facts = SourceFactsV2(
        rate=ir_v2.rate,
        sources=(SourceFactV2(source_id=source_id, duration_frames=total),),
    )
    episode_nouns = episode_root / PROPER_NOUNS_NAME
    options = (
        SubtitleBuildOptions(proper_nouns=merged_proper_nouns(episode_nouns))
        if episode_nouns.is_file()
        else SubtitleBuildOptions()
    )
    subtitle_plan = build_subtitle_plan(segments, source_facts=facts, ir_v2=ir_v2, options=options)

    latest = latest_selections(record)
    selections: dict[str, RecipeSelection] = {}
    snapshot: list[dict[str, str | None]] = []
    notes: list[str] = []
    audio_selection: RecipeSelection | None = None
    color_selection: RecipeSelection | None = None
    for domain in sorted(latest):
        entry = latest[domain]
        snapshot.append(
            {
                "domain": entry.domain,
                "recipe_id": entry.recipe_id,
                "semantic_intent": entry.semantic_intent,
            }
        )
        if entry.recipe_id is None:
            notes.append(f"kit selection ({domain}): none recorded — plan omitted")
            continue
        resolved = _resolve(kit, record, domain)
        if resolved is None:  # pragma: no cover - recipe_id present means non-None
            continue
        selections[resolved.recipe.semantic_intent] = resolved
        if domain == "audio":
            audio_selection = resolved
        elif domain == "color":
            color_selection = resolved

    audio_plan = _audio_plan(ir_v2, audio_selection, notes)
    color_plan = _color_plan(ir_v2, color_selection, source_id, notes)
    return FinishingPlans(
        subtitle_plan=subtitle_plan,
        audio_plan=audio_plan,
        color_plan=color_plan,
        recipe_selections=selections,
        kit_snapshot=tuple(snapshot),
        notes=tuple(notes),
    )


def _resolve(
    kit: ChannelProductionKitV1, record: KitSelectionRecordV1, domain: str
) -> RecipeSelection | None:
    try:
        return recipe_selection_from_record(kit, record, domain)
    except KitPreviewError as error:
        if error.code in ("unknown-recipe", "selection-ambiguous"):
            raise FinishingMalformedError(
                f"kit-selections-{error.code}", error.detail
            ) from error
        raise FinishingError(f"kit-selections-{error.code}", error.detail) from error


def _audio_plan(
    ir_v2: TimelineIrV2,
    selection: RecipeSelection | None,
    notes: list[str],
) -> AudioFinishingPlanV1 | None:
    if selection is None:
        return None
    intent = selection.recipe.semantic_intent
    requests: tuple[AudioOpRequestV1, ...] = ()
    if intent == "voice_isolation":
        requests = (
            AudioOpRequestV1(
                op="voice_isolation",
                justification=(
                    f"operator kit A/B selection (audio): {selection.recipe.recipe_id}"
                ),
            ),
        )
    elif intent == "music_cue_ducking":
        notes.append(
            "bgm-ducking selection recorded; the committed edit carries a dialogue "
            "mirror only — ducking ladder stages stay driven by BGM presence"
        )
    return build_audio_plan(
        AudioFactsV1(
            episode_id=ir_v2.episode_id,
            dialogue_clean=False,
            has_bgm=any(track.role == "music" for track in ir_v2.audio_tracks),
            has_ambience=any(track.role == "ambience" for track in ir_v2.audio_tracks),
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=requests,
    )


def _color_plan(
    ir_v2: TimelineIrV2,
    selection: RecipeSelection | None,
    source_id: str,
    notes: list[str],
) -> ColorFinishingPlanV1 | None:
    if selection is None:
        return None
    statuses = {
        row["capability"]: row["status"] for row in load_mcp_fit(MATRIX_PATH)["capabilities"]
    }
    valid = get_args(McpCapabilityStatus)
    grade = statuses["color-grade-preset-drx"]
    qc = statuses["advanced-delivery-qc"]
    if grade not in valid or qc not in valid:
        raise FinishingMalformedError(
            "invalid-matrix-status",
            f"color capability statuses must be one of {valid}: "
            f"color-grade-preset-drx={grade!r} advanced-delivery-qc={qc!r}",
        )
    intent = selection.recipe.semantic_intent
    exposure: tuple[ColorIssueV1, ...] = ()
    look_configured = False
    look_ref = None
    if intent == "color_technical_normalize":
        exposure = (
            ColorIssueV1(
                source_id=source_id,
                detail=f"operator kit A/B selection (color): {selection.recipe.recipe_id}",
            ),
        )
    elif intent == "color_look":
        look_configured = True
        look_ref = selection.recipe.recipe_id.replace("/", "-")
    else:  # pragma: no cover - the kit's color domain carries only these intents
        notes.append(f"color selection intent {intent!r} is not a color-plan driver")
    return build_color_plan(
        ColorFactsV1(
            episode_id=ir_v2.episode_id,
            exposure_issues=exposure,
            look_configured=look_configured,
            look_ref=look_ref,
        ),
        policy=ColorPlanPolicy(
            color_grade_status=cast("McpCapabilityStatus", grade),
            advanced_qc_status=cast("McpCapabilityStatus", qc),
        ),
    )


__all__ = ["build_finishing_plans"]
