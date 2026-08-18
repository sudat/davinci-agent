"""Strict SCHEMA parsing for Selection Plan Proposal documents (Todo 41).

Floats are rejected anywhere in the document, spans and evidence are
enforced by the Todo-40 strict models, and every malformed input
becomes a structured schema refusal instead of an exception escaping
the commit authority.
"""

from __future__ import annotations

import json
from typing import Final

from pydantic import ValidationError

from services.editorial.candidate_models import SelectionPlanProposal
from services.validate.selection_models import (
    SelectionValidationError,
    ValidationRefusal,
)

PROPOSAL_SCHEMA_VERSION: Final[str] = "selection-plan-proposal-v1"


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
            raise SelectionValidationError(
                ValidationRefusal(
                    validator="schema",
                    code="schema_invalid",
                    detail="floats are forbidden in selection plan proposals",
                )
            )
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list | tuple):
            stack.extend(node)


def _schema_refusal(detail: str) -> SelectionValidationError:
    return SelectionValidationError(
        ValidationRefusal(validator="schema", code="schema_invalid", detail=detail)
    )


def parse_selection_document(document: object) -> SelectionPlanProposal:
    """Strict parse; malformed input becomes a structured schema refusal."""

    if isinstance(document, SelectionPlanProposal):
        return document
    payload: object = document
    if isinstance(document, str | bytes):
        try:
            payload = json.loads(document)
        except ValueError as error:
            raise _schema_refusal(f"proposal document is not valid JSON: {error}") from error
    _reject_floats(payload)
    try:
        return SelectionPlanProposal.model_validate(tuplize(payload))
    except ValidationError as error:
        raise _schema_refusal(str(error)) from error


__all__ = [
    "PROPOSAL_SCHEMA_VERSION",
    "parse_selection_document",
    "tuplize",
]
