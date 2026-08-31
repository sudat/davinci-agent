"""CreativeEditPlanProposalV2 — full semantic edit-plan model (task 31).

PRD 8.11's complete semantic-operation set as typed operation variants in a
``kind``-discriminated union (table-driven dispatch — no if/elif chains on
variants anywhere in this package). Every op expresses WHAT editorial result
is wanted; raw Resolve API calls are forbidden (ResolveFreeModel rejects
``resolve*`` keys) and live only in task 38's McpExecutionPlan.

Task-28 lite mapping (upgrade_from_creative_draft): the pass-C
CreativeIntentV2 kinds absorb into the full ops as

    lite kind          -> full op (fields)
    ----------------------------------------------------------------
    (kept candidate)   -> PlacePrimaryClipOp (one per keep in span order,
                          still candidates -> PlaceStillOp anchored to the
                          previous (else next) placed keep)
    b_roll_insert      -> BRollOverlayOp (b-roll resolved via b_roll_matches;
                          anchor = the lite target speech candidate)
    subtitle_track     -> SubtitleTrackOp (target, all transcript segments)
    title_lower_third  -> TitleCardOp (lower_third, text=note as provisional
                          display text, duration = anchor record length)
    music_cue          -> MusicCueOp (target as-is, no asset)
    voice_cleanup      -> VoiceCleanupOp (voice_isolation — the talking-head
                          default; noise_cleanup is authorable directly)
    punch_in           -> PunchInOp (punch_in)
    color_look         -> ColorLookOp (look_ref=None = channel default)
    sfx_cue            -> SfxCueOp (asset_ref=None)
    manual_required    -> ManualRequiredOp (effect_note = lite note)
    cutaway/transition -> typed CreativePlanUpgradeError (the lite model
                          carries no cutaway-material/boundary pair; full
                          plans author these ops directly)

Speed-change ops are capability-gated at the TYPE level: only
``capability_verified=True`` is representable (Literal[True]), so an
unverified retime cannot even be parsed (PRD 8.11 "speed-change intent only
when capability verified").

Taste/presentation intents are NOT modeled here: the
``presentation_intents`` slot is a documented task-32 seam — free-form
``PresentationIntentRecordV2`` records today, the typed presentation-intent
schema replaces the record contents in task 32 without touching the plan
envelope.

Tuple coercion: every ``tuple`` field carries ``BeforeValidator(to_tuple)``
so JSON lists round-trip (cf. tasks 12/22/24).
"""

# allow: SIZE_OK — task 31 pins this deliverable to the two-file commit scope
# (edit_models_v2.py + compile_ir_v2.py); responsibility is single (the
# creative-edit-plan vocabulary). Task-23/29 single-module precedent.

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    ResolveFreeModel,
    Sha256,
    StrictModel,
    to_tuple,
)
from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentSelectionProposalV2,
)
from services.editorial_v2.prompt_v2 import (
    BrollMatchV2,
    CreativeEditDraft,
    CreativeIntentV2,
)

_Frames = Annotated[int, Field(gt=0, strict=True)]


# ------------------------------------------------------------ op variants


class PlacePrimaryClipOp(ResolveFreeModel):
    kind: Literal["place_primary_clip"]
    op_id: Identifier
    candidate_ref: Identifier


class BRollOverlayOp(ResolveFreeModel):
    kind: Literal["b_roll_overlay"]
    op_id: Identifier
    candidate_ref: Identifier
    anchor_candidate_ref: Identifier


class CutawayOp(ResolveFreeModel):
    kind: Literal["cutaway"]
    op_id: Identifier
    candidate_ref: Identifier
    anchor_candidate_ref: Identifier


class SplitEditOp(ResolveFreeModel):
    kind: Literal["split_edit"]
    op_id: Identifier
    style: Literal["j_cut", "l_cut"]
    a_candidate_ref: Identifier
    b_candidate_ref: Identifier
    lead_frames: _Frames


class SubtitleTrackOp(ResolveFreeModel):
    kind: Literal["subtitle_track"]
    op_id: Identifier
    target_candidate_ref: Identifier
    transcript_ref: Identifier | None = None


class TitleCardOp(ResolveFreeModel):
    kind: Literal["title_card"]
    op_id: Identifier
    variant: Literal["title", "lower_third", "chapter_card"]
    target_candidate_ref: Identifier
    text: Annotated[str, Field(min_length=1, strict=True)]
    duration_frames: _Frames


class PunchInOp(ResolveFreeModel):
    kind: Literal["punch_in"]
    op_id: Identifier
    target_candidate_ref: Identifier
    mode: Literal["punch_in", "crop"]


class PlaceStillOp(ResolveFreeModel):
    kind: Literal["place_still"]
    op_id: Identifier
    candidate_ref: Identifier
    anchor_candidate_ref: Identifier
    duration_frames: _Frames


class TransitionOp(ResolveFreeModel):
    kind: Literal["transition"]
    op_id: Identifier
    a_candidate_ref: Identifier
    b_candidate_ref: Identifier
    style: Literal["dissolve", "motion", "other"]
    duration_frames: _Frames


class MusicCueOp(ResolveFreeModel):
    kind: Literal["music_cue"]
    op_id: Identifier
    target_candidate_ref: Identifier | None = None
    asset_ref: Identifier | None = None
    ducking: bool = False


class VoiceCleanupOp(ResolveFreeModel):
    kind: Literal["voice_cleanup"]
    op_id: Identifier
    target_candidate_ref: Identifier | None = None
    treatment: Literal["voice_isolation", "noise_cleanup"]


class SfxCueOp(ResolveFreeModel):
    kind: Literal["sfx_cue"]
    op_id: Identifier
    target_candidate_ref: Identifier
    asset_ref: Identifier | None = None
    duration_frames: _Frames


class ColorLookOp(ResolveFreeModel):
    kind: Literal["color_look"]
    op_id: Identifier
    target_candidate_ref: Identifier | None = None
    look_ref: Identifier | None = None


class SpeedChangeOp(ResolveFreeModel):
    """Capability-flagged retime: only verified intents are representable."""

    kind: Literal["speed_change"]
    op_id: Identifier
    target_candidate_ref: Identifier
    factor_pct: _Frames
    capability_verified: Literal[True]


class ManualRequiredOp(ResolveFreeModel):
    kind: Literal["manual_required"]
    op_id: Identifier
    target_candidate_ref: Identifier | None = None
    effect_note: Annotated[str, Field(min_length=1, strict=True)]


EditOperationV2 = Annotated[
    PlacePrimaryClipOp
    | BRollOverlayOp
    | CutawayOp
    | SplitEditOp
    | SubtitleTrackOp
    | TitleCardOp
    | PunchInOp
    | PlaceStillOp
    | TransitionOp
    | MusicCueOp
    | VoiceCleanupOp
    | SfxCueOp
    | ColorLookOp
    | SpeedChangeOp
    | ManualRequiredOp,
    Field(discriminator="kind"),
]


# ------------------------------------------------------------ plan envelope


class PresentationIntentRecordV2(StrictModel):
    """Task-32 seam: free-form today, typed presentation-intent schema later."""

    intent_id: Identifier
    semantic_kind: Annotated[str, Field(min_length=1, strict=True)]
    note: str = ""
    target_candidate_ref: Identifier | None = None


class SelectionRefV2(StrictModel):
    """The moment-selection proposal this creative plan extends."""

    proposal_id: Identifier
    content_sha256: Sha256 | None = None


class CreativeEditPlanProposalV2(ResolveFreeModel):
    schema_version: Literal["creative-edit-plan-v2"]
    proposal_id: Identifier
    episode_id: Identifier
    selection_ref: SelectionRefV2
    operations: Annotated[tuple[EditOperationV2, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )
    presentation_intents: Annotated[
        tuple[PresentationIntentRecordV2, ...], BeforeValidator(to_tuple)
    ] = ()

    @model_validator(mode="after")
    def require_unique_ids(self) -> CreativeEditPlanProposalV2:
        op_ids = [op.op_id for op in self.operations]
        if len(set(op_ids)) != len(op_ids):
            raise PydanticCustomError("duplicate_op_id", "op ids are unique")
        intent_ids = [record.intent_id for record in self.presentation_intents]
        if len(set(intent_ids)) != len(intent_ids):
            raise PydanticCustomError("duplicate_intent_id", "intent ids are unique")
        return self


# ------------------------------------------------- lite draft -> full ops


class CreativePlanUpgradeError(ValueError):
    """The lite pass-C draft lacks data a full op needs (never guess)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def upgrade_from_creative_draft(
    draft: CreativeEditDraft,
    selection: MomentSelectionProposalV2,
    *,
    b_roll_matches: tuple[BrollMatchV2, ...] = (),
) -> tuple[EditOperationV2, ...]:
    """Absorb the task-28 lite draft into full ops (mapping table above)."""

    candidates = {c.candidate_id: c for c in selection.candidates}
    overlay_consumer = {match.b_roll_candidate_id for match in b_roll_matches}
    kept = sorted(
        (c for c in selection.candidates if c.intent == "keep"),
        key=lambda c: (c.source_span.start_frame, c.candidate_id),
    )
    ops: list[EditOperationV2] = []
    for candidate in kept:
        if candidate.candidate_id in overlay_consumer:
            continue
        if candidate.candidate_type == "still":
            ops.append(
                PlaceStillOp(
                    kind="place_still",
                    op_id=f"op-still-{candidate.candidate_id}",
                    candidate_ref=candidate.candidate_id,
                    anchor_candidate_ref=_still_anchor(candidate, kept, overlay_consumer),
                    duration_frames=(
                        candidate.source_span.end_frame - candidate.source_span.start_frame
                    ),
                )
            )
        else:
            ops.append(
                PlacePrimaryClipOp(
                    kind="place_primary_clip",
                    op_id=f"op-place-{candidate.candidate_id}",
                    candidate_ref=candidate.candidate_id,
                )
            )
    overlay_of: dict[str, str] = {}
    for match in b_roll_matches:
        overlay_of.setdefault(match.speech_candidate_id, match.b_roll_candidate_id)
    for intent in draft.intents:
        converter = _LITE_CONVERTERS.get(intent.kind)
        if converter is None:
            raise CreativePlanUpgradeError(
                "lite-kind-unmappable",
                f"lite intent {intent.intent_id} kind {intent.kind} carries too "
                "little data for a full op; author the op directly",
            )
        ops.append(converter(intent, candidates, overlay_of))
    return tuple(ops)


def _still_anchor(
    still: MomentCandidateV2,
    kept: tuple[MomentCandidateV2, ...] | list[MomentCandidateV2],
    overlay_consumer: set[str],
) -> str:
    placeable = [
        c
        for c in kept
        if c.candidate_id != still.candidate_id and c.candidate_id not in overlay_consumer
    ]
    before = [c for c in placeable if c.source_span.start_frame < still.source_span.start_frame]
    anchor = before[-1] if before else (placeable[0] if placeable else None)
    if anchor is None:
        raise CreativePlanUpgradeError(
            "still-without-anchor", f"still {still.candidate_id} has no placeable anchor"
        )
    return anchor.candidate_id


def _convert_subtitle(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> SubtitleTrackOp:
    if intent.target_candidate_id is None:
        raise CreativePlanUpgradeError(
            "subtitle-without-target", f"intent {intent.intent_id} has no target candidate"
        )
    return SubtitleTrackOp(
        kind="subtitle_track",
        op_id=f"op-sub-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
    )


def _convert_b_roll(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    overlays: dict[str, str],
) -> BRollOverlayOp:
    target = intent.target_candidate_id
    b_roll = overlays.get(target) if target is not None else None
    if target is None or b_roll is None:
        raise CreativePlanUpgradeError(
            "broll-without-match",
            f"intent {intent.intent_id} has no b_roll match for its target",
        )
    return BRollOverlayOp(
        kind="b_roll_overlay",
        op_id=f"op-broll-{intent.intent_id}",
        candidate_ref=b_roll,
        anchor_candidate_ref=target,
    )


def _convert_title(
    intent: CreativeIntentV2,
    candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> TitleCardOp:
    target = intent.target_candidate_id
    if target is None or target not in candidates:
        raise CreativePlanUpgradeError(
            "title-without-target", f"intent {intent.intent_id} has no known target"
        )
    span = candidates[target].source_span
    return TitleCardOp(
        kind="title_card",
        op_id=f"op-title-{intent.intent_id}",
        variant="lower_third",
        target_candidate_ref=target,
        text=intent.note,
        duration_frames=span.end_frame - span.start_frame,
    )


def _convert_music(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> MusicCueOp:
    return MusicCueOp(
        kind="music_cue",
        op_id=f"op-music-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
    )


def _convert_voice_cleanup(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> VoiceCleanupOp:
    return VoiceCleanupOp(
        kind="voice_cleanup",
        op_id=f"op-voice-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
        treatment="voice_isolation",
    )


def _convert_punch_in(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> PunchInOp:
    if intent.target_candidate_id is None:
        raise CreativePlanUpgradeError(
            "punch-in-without-target", f"intent {intent.intent_id} has no target"
        )
    return PunchInOp(
        kind="punch_in",
        op_id=f"op-punch-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
        mode="punch_in",
    )


def _convert_color_look(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> ColorLookOp:
    return ColorLookOp(
        kind="color_look",
        op_id=f"op-color-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
        look_ref=None,
    )


def _convert_sfx(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> SfxCueOp:
    if intent.target_candidate_id is None:
        raise CreativePlanUpgradeError(
            "sfx-without-target", f"intent {intent.intent_id} has no target"
        )
    return SfxCueOp(
        kind="sfx_cue",
        op_id=f"op-sfx-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
        duration_frames=_UPGRADED_SFX_FRAMES,
    )


def _convert_manual(
    intent: CreativeIntentV2,
    _candidates: dict[str, MomentCandidateV2],
    _overlays: dict[str, str],
) -> ManualRequiredOp:
    return ManualRequiredOp(
        kind="manual_required",
        op_id=f"op-manual-{intent.intent_id}",
        target_candidate_ref=intent.target_candidate_id,
        effect_note=intent.note,
    )


_UpgradeConverter = Callable[
    [CreativeIntentV2, dict[str, MomentCandidateV2], dict[str, str]], EditOperationV2
]

_UPGRADED_SFX_FRAMES: Final[int] = 12

_LITE_CONVERTERS: Final[dict[str, _UpgradeConverter]] = {
    "subtitle_track": _convert_subtitle,
    "b_roll_insert": _convert_b_roll,
    "title_lower_third": _convert_title,
    "music_cue": _convert_music,
    "voice_cleanup": _convert_voice_cleanup,
    "punch_in": _convert_punch_in,
    "color_look": _convert_color_look,
    "sfx_cue": _convert_sfx,
    "manual_required": _convert_manual,
    # "cutaway" and "transition" are intentionally absent: the lite model
    # carries no cutaway material / boundary pair — typed refusal, see table.
}


__all__ = [
    "BRollOverlayOp",
    "ColorLookOp",
    "CreativeEditPlanProposalV2",
    "CreativePlanUpgradeError",
    "CutawayOp",
    "EditOperationV2",
    "ManualRequiredOp",
    "MusicCueOp",
    "PlacePrimaryClipOp",
    "PlaceStillOp",
    "PresentationIntentRecordV2",
    "PunchInOp",
    "SelectionRefV2",
    "SfxCueOp",
    "SpeedChangeOp",
    "SplitEditOp",
    "SubtitleTrackOp",
    "TitleCardOp",
    "TransitionOp",
    "VoiceCleanupOp",
    "upgrade_from_creative_draft",
]
