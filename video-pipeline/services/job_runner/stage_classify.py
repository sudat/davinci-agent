"""Deterministic stage-failure classification table (PRD 6, Todo 11).

Pure table lookup — no AI, no heuristics, no state. Only codes
explicitly known to be transient may retry; every other code
(including unknown codes) classifies as permanent so an unclassified
fault can never loop (PRD 6: Retryはエラー種別ごとに決める).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from services.job_runner.stage_runner_models import FailureClass

TRANSIENT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"timeout", "transport_reset", "resolve_disconnect"}
)
PERMANENT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"schema_invalid", "missing_source", "capability_missing"}
)
BLOCKING_HUMAN_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"approval_required", "ambiguous_command"}
)


def classify_error(error_code: str) -> FailureClass:
    """Map one error code to its failure class; unknown codes never retry."""

    if error_code in TRANSIENT_ERROR_CODES:
        return "transient"
    if error_code in BLOCKING_HUMAN_ERROR_CODES:
        return "blocking_human"
    return "permanent"


__all__ = [
    "BLOCKING_HUMAN_ERROR_CODES",
    "PERMANENT_ERROR_CODES",
    "TRANSIENT_ERROR_CODES",
    "classify_error",
]
