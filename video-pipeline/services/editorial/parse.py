"""Strict structured-output parsing with responsibility guards (Todo 39).

The frozen Todo-32 schema is extra-forbid, so any invented key already fails
validation — but tool claims and commit/decision fields are rejected with
EXPLICIT error codes before schema validation so no forbidden responsibility
class can hide behind a generic schema violation. ``actor_intent`` must be
``model`` (human-only by the Literal contract), and segment references are
checked against the evidence bundle by the director (hallucination guard).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from services.contracts.editorial_model import EditorialSelectionProposal
from services.editorial.models import EditorialErrorRecord

FORBIDDEN_TOOL_KEYS: Final[frozenset[str]] = frozenset(
    {"tool_calls", "tools", "tool_use", "function_call", "shell", "bash", "subprocess"}
)
FORBIDDEN_COMMIT_KEYS: Final[frozenset[str]] = frozenset(
    {"commit", "committed", "decision", "decided", "approved", "approval", "finalize", "retry"}
)


@dataclass(frozen=True, slots=True)
class ParsedProposal:
    proposal: EditorialSelectionProposal


type ParseOutcome = ParsedProposal | EditorialErrorRecord


def _classify_decode_failure(
    text: str, error: json.JSONDecodeError
) -> Literal["truncated_response", "malformed_json"]:
    stripped_length = len(text.rstrip())
    if "Unterminated" in error.msg or (
        error.msg.startswith("Expecting") and error.pos >= stripped_length - 1
    ):
        return "truncated_response"
    return "malformed_json"


def _walk_forbidden_keys(node: object) -> str | None:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str):
                    lowered = key.lower()
                    if lowered in FORBIDDEN_TOOL_KEYS:
                        return f"arbitrary_tool_claim:{key}"
                    if lowered in FORBIDDEN_COMMIT_KEYS:
                        return f"commit_field_forbidden:{key}"
                stack.append(child)
        elif isinstance(current, list | tuple):
            stack.extend(current)
    return None


def parse_response(payload: bytes) -> ParseOutcome:
    """Parse one strict structured-output payload into a proposal or error."""

    text = payload.decode("utf-8", errors="replace")
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError as error:
        return EditorialErrorRecord(
            code=_classify_decode_failure(text, error),
            detail=error.msg,
        )
    if not isinstance(document, dict):
        return EditorialErrorRecord(
            code="malformed_json", detail="response is not a JSON object"
        )
    forbidden = _walk_forbidden_keys(document)
    if forbidden is not None:
        kind, _, key = forbidden.partition(":")
        if kind == "arbitrary_tool_claim":
            return EditorialErrorRecord(
                code="arbitrary_tool_claim",
                detail=(
                    f"response key {key!r} claims tool use; the editorial model has no "
                    "tool surface (no shell, no write, no network, no Resolve control)"
                ),
            )
        return EditorialErrorRecord(
            code="commit_field_forbidden",
            detail=(
                f"response key {key!r} carries a decision/commit-class field; the "
                "editorial model PROPOSES ONLY and can never commit, approve, or retry"
            ),
        )
    try:
        proposal = EditorialSelectionProposal.model_validate_json(text)
    except ValidationError as error:
        return EditorialErrorRecord(code="proposal_schema_violation", detail=str(error))
    return ParsedProposal(proposal=proposal)


__all__ = [
    "FORBIDDEN_COMMIT_KEYS",
    "FORBIDDEN_TOOL_KEYS",
    "ParsedProposal",
    "parse_response",
]
