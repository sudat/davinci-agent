"""Typed interruption policy for the episode cockpit (PRD 13.2.1).

Every potential interruption is classified into one of three levels —
``SAFE_AUTO_RESOLVE`` (accepted fallback, continue, record),
``DEGRADED_BUT_RECOVERABLE`` (bounded-quality fallback, continue,
surface in final review), ``HUMAN_DECISION_REQUIRED`` (material
editorial meaning, rights/privacy, destructive ambiguity, or
publication authority — stop the operator).

Routine technical interruptions must not babysit the operator one by
one, so the policy table maps each known kind to its level, a Japanese
operator notice, and the recorded resolution. Unknown kinds fail closed
to ``HUMAN_DECISION_REQUIRED`` rather than guessing: the event's
``kind`` therefore accepts any non-empty string instead of a closed
Literal, and the classifier is the single place that decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.models import (
    NonEmpty,  # noqa: TC001 (pydantic resolves this alias at class-definition runtime)
)


def _coerce_notices(value: object) -> object:
    """JSON round-trip: a dumped tuple arrives as a list; coerce it back."""
    if isinstance(value, list):
        return tuple(value)
    return value


type Notices = Annotated[tuple[str, ...], BeforeValidator(_coerce_notices)]

InterruptionLevel = Literal[
    "SAFE_AUTO_RESOLVE",
    "DEGRADED_BUT_RECOVERABLE",
    "HUMAN_DECISION_REQUIRED",
]

InterruptionKind = Literal[
    "subtitle_path_fallback",
    "mcp_transient_failure",
    "optional_effect_unavailable",
    "low_confidence_b_roll",
    "rights_ambiguity",
    "publication_approval",
]

_BLOCKING_LEVEL: InterruptionLevel = "HUMAN_DECISION_REQUIRED"
_LEVELS: tuple[InterruptionLevel, ...] = (
    "SAFE_AUTO_RESOLVE",
    "DEGRADED_BUT_RECOVERABLE",
    "HUMAN_DECISION_REQUIRED",
)


class _KindPolicy(NamedTuple):
    """Table entry: level, operator notice, recorded resolution."""

    level: InterruptionLevel
    notice_text: str
    resolution: str


_POLICIES: dict[str, _KindPolicy] = {
    "subtitle_path_fallback": _KindPolicy(
        "SAFE_AUTO_RESOLVE",
        "字幕経路を代替実装へ切り替えて自動継続しました（記録済み）。",  # noqa: RUF001 (JA notice)
        "字幕経路を代替実装へ切り替えて継続",
    ),
    "mcp_transient_failure": _KindPolicy(
        "SAFE_AUTO_RESOLVE",
        "MCP接続の一時的失敗を自動再試行しました（記録済み）。",  # noqa: RUF001 (JA notice)
        "一時的MCP失敗を自動再試行して継続",
    ),
    "optional_effect_unavailable": _KindPolicy(
        "SAFE_AUTO_RESOLVE",
        "任意効果のレシピが利用できないため、安全な低複雑度レシピで自動継続しました（記録済み）。",  # noqa: RUF001 (JA notice)
        "任意効果を低複雑度レシピへ切り替えて継続",
    ),
    "low_confidence_b_roll": _KindPolicy(
        "SAFE_AUTO_RESOLVE",
        "低confidenceのB-roll候補を安全に除外して自動継続しました（記録済み）。",  # noqa: RUF001 (JA notice)
        "低confidenceのB-roll候補を除外して継続",
    ),
    "rights_ambiguity": _KindPolicy(
        "HUMAN_DECISION_REQUIRED",
        "権利・プライバシーの方針を確定できません。人的判断のため処理を停止します。",
        "権利曖昧のため停止して人的判断を待つ",
    ),
    "publication_approval": _KindPolicy(
        "HUMAN_DECISION_REQUIRED",
        "公開には人の承認が必要です。公開ステップで停止します。",
        "公開承認のため停止して人的判断を待つ",
    ),
}

_UNKNOWN_POLICY = _KindPolicy(
    "HUMAN_DECISION_REQUIRED",
    "未知の割込み種別のため、安全側の判断として停止します（要確認）。",  # noqa: RUF001 (JA notice)
    "未知の割込み種別のためfail-closedで停止",
)


class InterruptionEvent(StrictModel):
    """One potential interruption observed by the pipeline.

    ``kind`` deliberately accepts any non-empty string (not the closed
    ``InterruptionKind`` Literal) so an unclassified interruption is
    classified fail-closed to HUMAN instead of crashing the pipeline.
    """

    kind: NonEmpty
    episode_id: Identifier | None = None
    stage: NonEmpty | None = None
    detail: NonEmpty | None = None


class InterruptionRecord(StrictModel):
    """Serializable audit record of one classified interruption."""

    kind: NonEmpty
    level: InterruptionLevel
    resolution: NonEmpty
    episode_id: Identifier | None = None
    stage: NonEmpty | None = None
    detail: NonEmpty | None = None


class InterruptionDecision(StrictModel):
    """Typed classification outcome for one interruption event."""

    kind: NonEmpty
    level: InterruptionLevel
    notice_text: NonEmpty
    record: InterruptionRecord
    blocks_operator: bool

    @model_validator(mode="after")
    def require_blocks_matches_level(self) -> InterruptionDecision:
        """Only HUMAN_DECISION_REQUIRED may block the operator."""
        if self.blocks_operator != (self.level == _BLOCKING_LEVEL):
            raise PydanticCustomError(
                "blocks_level_mismatch",
                "blocks_operator must equal (level == HUMAN_DECISION_REQUIRED)",
            )
        return self


class InterruptionSummary(StrictModel):
    """Aggregate over one run's decisions: per-level counts and notices."""

    counts: dict[InterruptionLevel, int]
    blocking_count: int
    notices: Notices

    @model_validator(mode="after")
    def require_all_levels_counted(self) -> InterruptionSummary:
        """Counts must cover all three levels (never a partial mapping)."""
        if set(self.counts) != set(_LEVELS):
            raise PydanticCustomError(
                "counts_incomplete",
                "counts must cover all three interruption levels",
            )
        return self


def classify_interruption(event: InterruptionEvent) -> InterruptionDecision:
    """Classify one interruption event into its typed three-level decision."""
    policy = _POLICIES.get(event.kind, _UNKNOWN_POLICY)
    record = InterruptionRecord(
        kind=event.kind,
        level=policy.level,
        resolution=policy.resolution,
        episode_id=event.episode_id,
        stage=event.stage,
        detail=event.detail,
    )
    return InterruptionDecision(
        kind=event.kind,
        level=policy.level,
        notice_text=policy.notice_text,
        record=record,
        blocks_operator=policy.level == _BLOCKING_LEVEL,
    )


def aggregate_decisions(decisions: Sequence[InterruptionDecision]) -> InterruptionSummary:
    """Aggregate decisions into per-level counts, blocking count, and notices."""
    counts: dict[InterruptionLevel, int] = dict.fromkeys(_LEVELS, 0)
    for decision in decisions:
        counts[decision.level] += 1
    return InterruptionSummary(
        counts=counts,
        blocking_count=counts[_BLOCKING_LEVEL],
        notices=tuple(decision.notice_text for decision in decisions),
    )


__all__ = [
    "InterruptionDecision",
    "InterruptionEvent",
    "InterruptionKind",
    "InterruptionLevel",
    "InterruptionRecord",
    "InterruptionSummary",
    "aggregate_decisions",
    "classify_interruption",
]
