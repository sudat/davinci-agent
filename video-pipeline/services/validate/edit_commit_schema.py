"""Strict SCHEMA parsing for Edit Plan proposal documents (Todo 43).

Floats are rejected anywhere in the document; Resolve-specific and
LLM-authored uuid keys are refused by the Todo-43 model base (Todo 26
precedent); every malformed input becomes a structured schema refusal
instead of an exception escaping the commit authority.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from services.plan.edit_plan_models import EditPlan
from services.validate.edit_commit_models import (
    EditPlanValidationError,
    EditValidationRefusal,
)


def tuplize(value: object) -> object:
    """Coerce parsed JSON arrays to tuples for strict contract models."""

    if isinstance(value, list):
        return tuple(tuplize(item) for item in value)
    if isinstance(value, dict):
        return {key: tuplize(item) for key, item in value.items()}
    return value


def _reject_floats(value: object) -> None:
    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, float):
            raise EditPlanValidationError(
                EditValidationRefusal(
                    validator="schema",
                    code="schema_invalid",
                    detail="floats are forbidden in edit plan proposals",
                )
            )
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list | tuple):
            stack.extend(node)


def _schema_refusal(detail: str) -> EditPlanValidationError:
    return EditPlanValidationError(
        EditValidationRefusal(validator="schema", code="schema_invalid", detail=detail)
    )


def parse_edit_plan_document(document: object) -> EditPlan:
    """Strict parse; malformed input becomes a structured schema refusal."""

    if isinstance(document, EditPlan):
        return document
    payload: object = document
    if isinstance(document, str | bytes):
        try:
            payload = json.loads(document)
        except ValueError as error:
            raise _schema_refusal(f"edit plan document is not valid JSON: {error}") from error
    _reject_floats(payload)
    try:
        return EditPlan.model_validate(tuplize(payload))
    except ValidationError as error:
        raise _schema_refusal(str(error)) from error


__all__ = ["parse_edit_plan_document", "tuplize"]
