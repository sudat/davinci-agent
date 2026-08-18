"""Deterministic Edit Plan identity hashing (Todo 43).

``compute_decision_id`` and ``compute_edit_item_id`` derive stable sha256
identities over the CANONICAL serialization of normalized fields — the
decision over (base selection plan, candidate, kind, parent), the item over
(source, span, track kind, decision). Decision identity is RECOMPUTABLE from
the planner inputs alone: LLM-authored ``llm_uuid``/``decision_uuid`` keys are
rejected before any model construction (LLM UUID continuity is never trusted,
PRD 14.2).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from services.editorial.candidate_models import CandidateSpan

FORBIDDEN_UUID_KEY_TOKENS: Final[tuple[str, ...]] = ("llm_uuid", "decision_uuid")


def offending_uuid_key(node: object) -> str | None:
    """The first key carrying an LLM-authored uuid token, if any."""

    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str) and any(
                    token in key.lower() for token in FORBIDDEN_UUID_KEY_TOKENS
                ):
                    return key
                stack.append(child)
        elif isinstance(current, list | tuple):
            stack.extend(current)
    return None


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _span_fields(span: CandidateSpan) -> dict[str, int]:
    return {
        "end_frame": span.end_frame,
        "rate_den": span.rate_den,
        "rate_num": span.rate_num,
        "start_frame": span.start_frame,
    }


def compute_decision_id(  # noqa: PLR0913 (identity fields are the record)
    *,
    base_episode_id: str,
    base_plan_version: str,
    base_plan_sha256: str,
    candidate_id: str,
    kind: str,
    parent_candidate_id: str | None,
) -> str:
    """STABLE deterministic ID over the decision identity fields."""

    payload: dict[str, object] = {
        "base_selection": {
            "episode_id": base_episode_id,
            "plan_sha256": base_plan_sha256,
            "plan_version": base_plan_version,
        },
        "candidate_id": candidate_id,
        "kind": kind,
        "parent_candidate_id": parent_candidate_id,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def compute_edit_item_id(
    *,
    source_id: str,
    edit_source_sha: str,
    span: CandidateSpan,
    track_kind: str,
    decision_id: str,
) -> str:
    """STABLE deterministic ID over the item identity fields."""

    payload: dict[str, object] = {
        "decision_id": decision_id,
        "source": {"source_id": source_id, "source_sha": edit_source_sha},
        "span": _span_fields(span),
        "track_kind": track_kind,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


__all__ = [
    "FORBIDDEN_UUID_KEY_TOKENS",
    "compute_decision_id",
    "compute_edit_item_id",
    "offending_uuid_key",
]
