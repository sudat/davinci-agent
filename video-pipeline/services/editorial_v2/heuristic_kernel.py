"""Heuristic judgment kernel for Director v2 (task 28).

Pure decision primitives shared by the planner passes: the deterministic
keyword tokenizer, the 13-dimension scorer (PRD 8.2), the ordered keep/
remove/optional decision ladder (quiet scenes NOT auto-penalized — the
low-energy rung precedes the remove rung), low-energy role detection, and
redundancy grouping. No I/O, no clock, no randomness.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, NamedTuple

from services.editorial_v2.prompt_v2 import (
    BrollMatchV2,
    EditorialDimension,
    LowEnergyRole,
)

if TYPE_CHECKING:
    from services.editorial_v2.moment_models import MomentCandidateType, MomentCandidateV2

_POTENTIAL_VALUE: Final[dict[str, float]] = {"high": 0.85, "medium": 0.55, "low": 0.3}
_NOVELTY: Final[dict[str, float]] = {"high": 0.7, "medium": 0.5, "low": 0.3}
_CUTTABILITY: Final[dict[str, float]] = {"high": 0.8, "medium": 0.6, "low": 0.5}
_NON_SPEECH_VALUABLE: Final[frozenset[str]] = frozenset({
    "b_roll", "reaction", "action", "establishing", "insert",
    "product_demo", "screen_demo",
})
_TENSION: Final = frozenset({"tension", "suspense", "緊張"})
_TRANSITION_WORDS: Final = frozenset({"transition", "転換"})
_REFLECTION: Final = frozenset({"reflection", "夜景"})
_QUIET: Final = frozenset({"quiet", "silent", "dusk", "night", "breather", "静か"})
_CONFIRMED_METHODS: Final = 2
_REDUNDANCY_JACCARD: Final = 0.5
_WORD: Final = re.compile(r"[a-z0-9]{3,}")
_CJK: Final = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


class Facts(NamedTuple):
    """Per-candidate judgment inputs derived from api_v2 digest rows."""

    candidate_type: MomentCandidateType
    potential: str
    quiet: LowEnergyRole | None
    must: bool
    methods: int
    grouped: bool
    assigned: bool
    taste_refs: tuple[str, ...]


def tokens(text: str) -> frozenset[str]:
    """Deterministic keyword kernel: ascii words (>=3) + CJK bigrams."""

    folded = text.casefold()
    words = frozenset(_WORD.findall(folded))
    bigrams = frozenset(
        folded[i : i + 2]
        for i in range(len(folded) - 1)
        if _CJK.match(folded[i]) and _CJK.match(folded[i + 1])
    )
    return words | bigrams


def low_energy_role(description: str) -> LowEnergyRole | None:
    tokens_ = tokens(description)
    if tokens_ & _TENSION:
        return "tension"
    if tokens_ & _TRANSITION_WORDS:
        return "transition"
    if tokens_ & _REFLECTION:
        return "reflection"
    if tokens_ & _QUIET:
        return "breathing_room"
    return None


def dimension_scores(facts: Facts) -> dict[EditorialDimension, float]:
    """All 13 value scores (higher = better for the edit; redundancy means
    freedom-from-redundancy)."""

    base = _POTENTIAL_VALUE.get(facts.potential, 0.5)
    return {
        EditorialDimension.information_value: base,
        EditorialDimension.story_progression: 0.7 if facts.assigned else 0.4,
        EditorialDimension.novelty: _NOVELTY.get(facts.potential, 0.5),
        EditorialDimension.emotional_energy: base - 0.15,
        EditorialDimension.authenticity: 0.6,
        EditorialDimension.humor_surprise: 0.4,
        EditorialDimension.visual_interest: (
            0.7 if facts.candidate_type in _NON_SPEECH_VALUABLE else 0.5
        ),
        EditorialDimension.clarity: 0.6,
        EditorialDimension.redundancy: 0.35 if facts.grouped else 1.0,
        EditorialDimension.technical_usability: (
            0.9 if facts.methods >= _CONFIRMED_METHODS else 0.6
        ),
        EditorialDimension.continuity_cuttability: _CUTTABILITY.get(facts.potential, 0.5),
        EditorialDimension.brief_relationship: 0.85 if facts.must else base,
        EditorialDimension.channel_style: 0.5,
    }


def decide(facts: Facts) -> tuple[str, str]:  # noqa: PLR0911 (ladder: each rung names its rule)
    if facts.must:
        return "keep", "must-include per brief: matches a brief idea/moments entry"
    if facts.potential == "high":
        return "keep", "high information value and story progression"
    if facts.quiet is not None and facts.potential != "low":
        return "keep", (
            f"low-energy value as {facts.quiet}: tension/reflection/transition/breathing room"
        )
    if (
        facts.candidate_type in _NON_SPEECH_VALUABLE
        and facts.potential != "low"
        and facts.methods >= _CONFIRMED_METHODS
    ):
        return "keep", (
            f"valuable non-speech visual evidence corroborated by {facts.methods} methods"
        )
    if facts.taste_refs:
        return "optional", "borderline B-roll kept available per cited taste for B-roll density"
    if facts.potential == "low" and facts.quiet is None:
        return "remove", (
            "low information value; redundant with better-covered material; "
            "weak relationship to the brief"
        )
    return "optional", "borderline value; available as an alternate"


def redundancy_groups(
    candidates: tuple[MomentCandidateV2, ...], descriptions: dict[str, str]
) -> dict[str, str]:
    """Pairwise same-type description-overlap groups (Jaccard threshold)."""

    groups: dict[str, str] = {}
    tokens_of = {shot_id: tokens(text) for shot_id, text in descriptions.items()}
    for index, first in enumerate(candidates):
        if first.candidate_id in groups:
            continue
        for second in candidates[index + 1 :]:
            if second.candidate_id in groups or first.candidate_type != second.candidate_type:
                continue
            left = tokens_of.get(first.evidence_refs[0], frozenset())
            right = tokens_of.get(second.evidence_refs[0], frozenset())
            union = left | right
            if union and len(left & right) / len(union) >= _REDUNDANCY_JACCARD:
                group = f"rg-{first.evidence_refs[0]}"
                groups[first.candidate_id] = group
                groups[second.candidate_id] = group
    return groups


def semantic_b_roll_matches(
    speech: tuple[MomentCandidateV2, ...],
    b_rolls: tuple[MomentCandidateV2, ...],
    descriptions: dict[str, str],
) -> tuple[BrollMatchV2, ...]:
    """Pair kept/optional speech with B-roll sharing description terms."""

    shot_tokens = {shot_id: tokens(text) for shot_id, text in descriptions.items()}
    empty = frozenset()
    return tuple(
        BrollMatchV2(
            speech_candidate_id=talker.candidate_id,
            b_roll_candidate_id=b_roll.candidate_id,
            shared_terms=tuple(sorted(
                shot_tokens.get(talker.evidence_refs[0], empty)
                & shot_tokens.get(b_roll.evidence_refs[0], empty)
            )[:5]),
        )
        for talker in speech
        for b_roll in b_rolls
        if shot_tokens.get(talker.evidence_refs[0], empty)
        & shot_tokens.get(b_roll.evidence_refs[0], empty)
    )


__all__ = [
    "Facts",
    "decide",
    "dimension_scores",
    "low_energy_role",
    "redundancy_groups",
    "semantic_b_roll_matches",
    "tokens",
]
