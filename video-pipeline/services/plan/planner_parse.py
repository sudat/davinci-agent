"""Strict parsing for planner input documents (Todo 42).

The MODEL may propose meaning and weights, but the SOLVER is pure code and
cannot be overridden: any document carrying model-authored override keys
(``solver``/``override``/``force``/``solution`` tokens anywhere in a key
name) is typed-rejected BEFORE any solve, and floats are rejected anywhere
(no float durations can enter the frame arithmetic). Malformed documents
become a typed :class:`PlannerInputError` instead of an escaping exception.
"""

from __future__ import annotations

import json
from typing import Final, Literal

from pydantic import ValidationError

from services.plan.planner_models import PlannerInput
from services.validate.selection_schema import tuplize

PlannerInputErrorCode = Literal["solver_override", "schema"]

FORBIDDEN_INPUT_KEY_TOKENS: Final[tuple[str, ...]] = (
    "solver",
    "override",
    "solution",
)


def _is_forbidden_key(key: str) -> bool:
    """Substring tokens plus ``force*`` keys; ``enforcement`` stays legal."""

    lowered = key.lower()
    if any(token in lowered for token in FORBIDDEN_INPUT_KEY_TOKENS):
        return True
    return lowered.startswith("force")


class PlannerInputError(Exception):
    """A planner input document was refused before any solve happened."""

    def __init__(self, code: PlannerInputErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _reject_model_authored_overrides(node: object) -> None:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str) and _is_forbidden_key(key):
                    raise PlannerInputError(
                        "solver_override",
                        f"input key {key!r} carries a model-authored "
                        "solver override; the model proposes weights, the "
                        "solver solves and cannot be overridden",
                    )
                stack.append(child)
        elif isinstance(current, list | tuple):
            stack.extend(current)


def _reject_floats(node: object) -> None:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            raise PlannerInputError(
                "schema", "floats are forbidden in planner inputs (integer frames only)"
            )
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list | tuple):
            stack.extend(current)


def parse_planner_input(document: object) -> PlannerInput:
    """Strict parse; override/float/malformed inputs are typed-rejected."""

    payload: object = document
    if isinstance(document, str | bytes):
        try:
            payload = json.loads(document)
        except ValueError as error:
            raise PlannerInputError(
                "schema", f"planner input document is not valid JSON: {error}"
            ) from error
    _reject_model_authored_overrides(payload)
    _reject_floats(payload)
    try:
        return PlannerInput.model_validate(tuplize(payload))
    except ValidationError as error:
        raise PlannerInputError("schema", str(error)) from error


__all__ = [
    "FORBIDDEN_INPUT_KEY_TOKENS",
    "PlannerInputError",
    "PlannerInputErrorCode",
    "parse_planner_input",
]
