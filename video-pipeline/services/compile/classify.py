from __future__ import annotations

from dataclasses import dataclass

from services.contracts.edit_plan_0c import (
    Classification0C,
    ConflictRecord0C,
    Decision0C,
    EditPlan0C,
    ItemIdSelector0C,
    LockedField0C,
    ReviewCommand0C,
    SubtitleTextSelector0C,
)

OPERATION_LOCKED_FIELD: dict[str, LockedField0C] = {
    "remove_segment": "order",
    "adjust_source_span": "span",
    "correct_subtitle": "text",
}
AMBIGUITY_CANDIDATE_THRESHOLD = 2


class ClassifyError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: Classification0C
    candidate_item_ids: tuple[str, ...]
    conflict: ConflictRecord0C | None


def resolve_candidates(plan: EditPlan0C, command: ReviewCommand0C) -> tuple[str, ...]:
    target = command.target
    if isinstance(target, ItemIdSelector0C):
        return tuple(item.item_id for item in plan.plan.items if item.item_id == target.item_id)
    if isinstance(target, SubtitleTextSelector0C):
        return tuple(
            item.item_id
            for item in plan.plan.items
            if item.kind == "subtitle" and item.subtitle_text == target.text
        )
    raise ClassifyError(f"unsupported target selector for command {command.command_id}")


def classify_command(plan: EditPlan0C, command: ReviewCommand0C) -> ClassificationResult:
    if command.base_plan_version != plan.plan.plan_version:
        raise ClassifyError(
            f"command targets plan {command.base_plan_version} "
            f"but the current plan is {plan.plan.plan_version}"
        )
    candidates = resolve_candidates(plan, command)
    if not candidates:
        raise ClassifyError(
            f"unresolved anchor: no plan item matches command {command.command_id}"
        )
    if len(candidates) >= AMBIGUITY_CANDIDATE_THRESHOLD:
        return ClassificationResult("ambiguous", candidates, None)
    items_by_id = {item.item_id: item for item in plan.plan.items}
    target_item = items_by_id[candidates[0]]
    locked_field = OPERATION_LOCKED_FIELD[command.operation]
    if locked_field in target_item.locked_fields:
        return ClassificationResult(
            "conflict",
            candidates,
            ConflictRecord0C(
                command_id=command.command_id,
                target_item_id=target_item.item_id,
                locked_field=locked_field,
            ),
        )
    return ClassificationResult("clear", candidates, None)


def decide(plan: EditPlan0C, command: ReviewCommand0C) -> Decision0C:
    result = classify_command(plan, command)
    action = "apply" if result.classification == "clear" else "defer"
    return Decision0C(
        command_id=command.command_id,
        classification=result.classification,
        action=action,
        base_plan_version=command.base_plan_version,
        resulting_plan_version=(
            f"v{int(command.base_plan_version[1:]) + 1}" if action == "apply"
            else command.base_plan_version
        ),
        conflict=result.conflict,
        target_candidate_item_ids=result.candidate_item_ids,
    )
