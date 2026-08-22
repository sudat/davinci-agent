"""Production-kit recipe emitter (task 38).

Presentation intents become recipe-bound steps via task-37 selections.
The accepted_only binding is a HARD GATE (task-37 ``resolve_binding``
semantics): a recipe requiring an accepted capability that the matrix does
not accept refuses the whole compile — the step is never silently
downgraded or skipped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from services.mcp_execution.plan_models import (
    CompileExecutionPlanError,
    McpExecutionStepV1,
    ToolSurface,
)
from services.mcp_execution.plan_payloads import KitRecipeParams, RecipeParamsReadback
from services.mcp_execution.step_builders import (
    CapabilityView,
    mcp_or_executor,
    step_from,
)
from services.production_kit.capability_bindings import (
    CapabilityNotAcceptedError,
    CapabilityNotFoundError,
    resolve_binding,
)

if TYPE_CHECKING:
    from services.creative_plan.presentation_intents import PresentationIntentV2
    from services.production_kit.recipe_select import RecipeSelection

_KIND_SURFACE: Final[dict[str, ToolSurface]] = {
    "emphasis_punch_in": "set_transform",
    "broll_cutaway": "append_to_timeline",
    "lower_third": "insert_fusion_title",
    "keyword_text": "set_title_text",
    "chapter_card": "insert_fusion_title",
    "simple_dissolve": "duplicate_clips",
    "motion_transition": "duplicate_clips",
    "picture_in_picture": "set_transform",
    "screen_highlight": "insert_fusion_composition",
    "sfx_accent": "append_to_timeline",
}


def kit_steps(
    intents: Sequence[PresentationIntentV2],
    kit_selections: Mapping[str, RecipeSelection],
    caps: CapabilityView,
) -> list[McpExecutionStepV1]:
    steps: list[McpExecutionStepV1] = []
    for intent in sorted(intents, key=lambda i: i.intent_id):
        selection = kit_selections.get(intent.kind)
        if selection is None:
            raise CompileExecutionPlanError(
                "missing-kit-selection",
                f"presentation intent {intent.intent_id!r} kind {intent.kind!r} has no "
                "task-37 recipe selection",
            )
        recipe = selection.recipe
        try:
            resolve_binding(recipe, caps.as_mcp_fit())
        except CapabilityNotAcceptedError as error:
            raise CompileExecutionPlanError("capability-not-accepted", str(error)) from error
        except CapabilityNotFoundError as error:
            raise CompileExecutionPlanError("capability-unknown", str(error)) from error
        rung, record = caps.rung_for(
            recipe.capability_binding.capability, f"kit recipe {recipe.recipe_id}"
        )
        resolved = dict(sorted(selection.resolved_params.items()))
        steps.append(
            step_from(
                f"stp-kit-{intent.intent_id}",
                KitRecipeParams(
                    action="apply_kit_recipe",
                    intent_id=intent.intent_id,
                    kind=intent.kind,
                    recipe_id=recipe.recipe_id,
                    resolved_params=resolved,
                    rationale=selection.selection_rationale,
                    target_span=intent.target_span,
                    note=intent.rationale,
                ),
                RecipeParamsReadback(
                    kind="recipe_params", recipe_id=recipe.recipe_id, param_names=tuple(resolved)
                ),
                mcp_or_executor(rung, _KIND_SURFACE[intent.kind]),
                rung,
                record,
                intent.target_span.start_frame,
                timeline_ready=True,
            )
        )
    return steps


__all__ = ["kit_steps"]
