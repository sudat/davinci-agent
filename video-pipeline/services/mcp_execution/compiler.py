"""McpExecutionPlan compiler — IR v2 + creative plans -> steps (task 38).

``compile_execution_plan`` maps EVERY Timeline IR v2 element and committed
finishing plan onto typed steps (PRD §11.3): primary placements ->
import + place; b_roll/insert/still -> overlay; graphic -> title insert;
audio items -> audio placements; subtitle plan -> one path step per the
task-33 capability ladder; audio ladder enabled stages -> audio steps
(voice-isolation gated by its accepted matrix row); color needed sections
-> grade steps per the task-35 preferred path; effect intents and semantic
transitions -> their per-kind steps; presentation intents -> production-kit
recipe steps via task-37 selections (the accepted_only hard gate refuses
the step). Failed capabilities (task 11: transition-path,
audio-property-operation, bgm-track-ducking, title-text-plus, ...) map to
their matrix fallback rung WITH a fallback record — the plan stays
complete and the downgrade is explicit (PRD §19: report, never silently
downgrade).

Determinism: steps sort by ``(STEP_CLASS_RANK[action], record_position,
audio_order, step_id)`` and ``plan_id`` is the sha256 of the canonical bytes with the id
zeroed — the same inputs yield IDENTICAL canonical bytes (locked by
tests). There is no LLM path and no raw-call constructor: the step action
vocabulary is closed and the normalized-params union is discriminated, so
a free-form step cannot validate (surface frozen by test).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_execution.effect_steps import effect_steps, transition_steps
from services.mcp_execution.kit_steps import kit_steps
from services.mcp_execution.placement_steps import (
    audio_track_steps,
    import_steps,
    prepare_step,
    subtitle_step,
    telop_step,
    video_steps,
)
from services.mcp_execution.plan_models import (
    CompileExecutionPlanError,
    McpExecutionPlanV1,
    McpExecutionStepV1,
    compute_plan_id,
    step_sort_key,
)
from services.mcp_execution.plan_steps import audio_plan_steps, color_steps, render_native_steps
from services.mcp_execution.step_builders import CapabilityView, timeline_end
from services.toolchain.mcp_fit import load_mcp_fit

if TYPE_CHECKING:
    from services.creative_plan.audio_finishing import AudioFinishingPlanV1
    from services.creative_plan.color_finishing import ColorFinishingPlanV1
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.creative_plan.presentation_intents import PresentationIntentV2
    from services.creative_plan.subtitle_models import SubtitlePlanV1
    from services.mcp_execution.plan_payloads import TelopCardPayload
    from services.production_kit.recipe_select import RecipeSelection

_MATRIX_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "capabilities" / "v4.3" / "mcp-fit.json"
)

_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"accepted", "failed", "partial", "not_available"}
)

def _load_matrix() -> tuple[dict[str, str], dict[str, str]]:
    document = load_mcp_fit(_MATRIX_PATH)
    statuses: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    for row in document["capabilities"]:
        statuses[row["capability"]] = row["status"]
        fallbacks[row["capability"]] = row.get("fallback", "legacy_direct")
    return statuses, fallbacks


def compile_execution_plan(  # noqa: PLR0913 (task-mandated compiler signature)
    ir_v2: TimelineIrV2,
    *,
    subtitle_plan: SubtitlePlanV1,
    audio_plan: AudioFinishingPlanV1,
    color_plan: ColorFinishingPlanV1,
    presentation_intents: Sequence[PresentationIntentV2],
    kit_selections: Mapping[str, RecipeSelection],
    capability_statuses: Mapping[str, str] | None = None,
    telop_cards: Sequence[TelopCardPayload] = (),
) -> McpExecutionPlanV1:
    """Compile IR v2 + the four committed plans into a deterministic plan.

    ``capability_statuses`` overrides the real mcp-fit matrix rows for
    hermetic tests (status values only; fallback rungs then default to the
    direct scripting gap adapter). ``telop_cards`` carries the committed
    native-telop cards (DESIGN telop-nested WBS-3); empty (the default)
    emits no telop leg, so pre-telop plans compile byte-identically.
    """
    episode = ir_v2.episode_id
    for artifact, label in (
        (subtitle_plan, "subtitle"),
        (audio_plan, "audio"),
        (color_plan, "color"),
    ):
        if artifact.episode_id != episode:
            raise CompileExecutionPlanError(
                "episode-mismatch",
                f"{label} plan episode {artifact.episode_id!r} != IR episode {episode!r}",
            )
    statuses, fallbacks = (
        _load_matrix() if capability_statuses is None else (dict(capability_statuses), {})
    )
    invalid = sorted(
        f"{capability}={status!r}"
        for capability, status in statuses.items()
        if status not in _VALID_STATUSES
    )
    if invalid:
        raise CompileExecutionPlanError(
            "invalid-matrix-status", f"unknown capability statuses: {', '.join(invalid)}"
        )
    caps = CapabilityView(statuses, fallbacks)

    intents_by_id = {intent.intent_id: intent for intent in presentation_intents}
    resolved_intents: list[PresentationIntentV2] = []
    for ref in ir_v2.presentation_intent_refs:
        intent = intents_by_id.get(ref)
        if intent is None:
            raise CompileExecutionPlanError(
                "unknown-intent",
                f"presentation intent {ref!r} referenced by the IR is not among the "
                "provided intents",
            )
        resolved_intents.append(intent)

    wide = timeline_end(ir_v2)
    steps: list[McpExecutionStepV1] = []
    # Kit gate first: a not-accepted accepted_only binding refuses the whole
    # compile before any other step is minted.
    steps.extend(kit_steps(resolved_intents, kit_selections, caps))
    steps.append(prepare_step(ir_v2, caps))
    steps.extend(import_steps(ir_v2, caps))
    steps.extend(video_steps(ir_v2, caps))
    steps.extend(audio_track_steps(ir_v2, caps))
    steps.append(subtitle_step(subtitle_plan, caps))
    if telop_cards:
        steps.append(telop_step(telop_cards))
    steps.extend(
        effect_steps(
            ir_v2,
            caps,
            wide=wide,
            color_look_covered=color_plan.channel_episode_look.needed,
        )
    )
    steps.extend(transition_steps(ir_v2, caps))
    steps.extend(audio_plan_steps(audio_plan, caps, wide))
    steps.extend(color_steps(color_plan, caps, wide, ir_v2))
    steps.extend(render_native_steps(ir_v2, caps, wide))
    ordered = tuple(sorted(steps, key=step_sort_key))
    return McpExecutionPlanV1(
        schema_version="mcp-execution-plan-v1",
        plan_id=compute_plan_id(episode, ordered),
        episode_id=episode,
        steps=ordered,
    )


__all__ = [
    "CompileExecutionPlanError",
    "compile_execution_plan",
]
