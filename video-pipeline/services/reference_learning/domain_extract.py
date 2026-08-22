"""Domain-scoped polarity extraction — v43 task 26.

Deterministic keyword/pattern core + LLM seam with schema post-validation.

The deterministic core must pass canonical Japanese + English cases without
any network/LLM. Comment text is always DATA — never executed as instruction.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.reference_learning.models import Polarity, PreferenceDomain

# ---------------------------------------------------------------------------
# Shared helpers — coercion mirrors models.py so JSON round-trip works
# ---------------------------------------------------------------------------


def _tuple_domains(value: object) -> object:
    if isinstance(value, list):
        out: list[PreferenceDomain] = []
        for item in value:
            if isinstance(item, str):
                out.append(PreferenceDomain(item))
            else:
                out.append(item)  # type: ignore[arg-type]
        return tuple(out)
    return value


def _coerce_domains_dict(value: object) -> object:
    if isinstance(value, dict):
        coerced: dict[PreferenceDomain, Polarity] = {}
        for k, v in value.items():  # type: ignore[union-attr]
            key = PreferenceDomain(k) if isinstance(k, str) else k
            coerced[key] = v  # type: ignore[index]
        return coerced
    return value


# ---------------------------------------------------------------------------
# ParsedAnnotationDraft — compatible with ReferenceAnnotationV1 scoping
# ---------------------------------------------------------------------------


class ParsedAnnotationDraft(StrictModel):
    """Deterministic/LLM draft before full ReferenceAnnotationV1 commit.

    Hard scoping rule: any domain not in ``named_domains`` must not carry
    a non-unspecified polarity (validated here, not only in callers).
    """

    named_domains: Annotated[
        tuple[PreferenceDomain, ...], BeforeValidator(_tuple_domains)
    ] = ()
    domains: Annotated[
        dict[PreferenceDomain, Polarity], BeforeValidator(_coerce_domains_dict)
    ] = Field(default_factory=dict)
    needs_review: bool = False
    rationale: Annotated[str, Field(min_length=1, strict=True)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8

    @model_validator(mode="after")
    def enforce_scoping(self) -> ParsedAnnotationDraft:
        if len(set(self.named_domains)) != len(self.named_domains):
            raise PydanticCustomError(
                "duplicate_named_domain", "named_domains must be unique"
            )
        named_set = set(self.named_domains)
        # polarity without domain
        if not named_set:
            for dom, pol in self.domains.items():
                if pol != "unspecified":
                    raise PydanticCustomError(
                        "polarity_without_domain",
                        "polarity {pol} on {domain} but no domain was named",
                        {"pol": pol, "domain": str(dom)},
                    )
            return self
        for dom, pol in self.domains.items():
            if dom not in named_set and pol != "unspecified":
                raise PydanticCustomError(
                    "domain_not_named",
                    "domain {domain} has polarity {pol} but was not named",
                    {"domain": str(dom), "pol": pol},
                )
        for nd in named_set:
            pol = self.domains.get(nd)
            if pol is None:
                raise PydanticCustomError(
                    "named_domain_missing",
                    "named domain {domain} has no polarity entry",
                    {"domain": str(nd)},
                )
            if pol == "unspecified":
                raise PydanticCustomError(
                    "named_domain_unspecified",
                    "named domain {domain} must not be unspecified",
                    {"domain": str(nd)},
                )
        return self


# ---------------------------------------------------------------------------
# Deterministic keyword core
# ---------------------------------------------------------------------------

_DOMAIN_KEYWORDS: dict[PreferenceDomain, tuple[str, ...]] = {
    PreferenceDomain.color: ("色", "カラー", "色彩", "color", "colour"),
    PreferenceDomain.pacing: ("ペーシング", "ペースング", "テンポ", "ペース", "pacing", "pace"),
    PreferenceDomain.subtitle: ("字幕", "テロップ", "subtitle", "subtitles", "caption", "captions"),
    PreferenceDomain.story_structure: (
        "ストーリー",
        "構成",
        "話の構成",
        "story",
        "structure",
        "thesis",
        "hook",
    ),
    PreferenceDomain.b_roll: ("b-roll", "b roll", "b_roll", "bロール", "broll"),
    PreferenceDomain.framing_graphics: (
        "フレーミング",
        "構図",
        "フレーム",
        "graphics",
        "framing",
        "punch-in",
        "punch in",
        "lower third",
        "lower-third",
        "タイトル",
        "graphic",
    ),
    PreferenceDomain.audio: (
        "オーディオ",
        "音楽",
        "サウンド",
        "音声",
        "ナレーション",
        "audio",
        "bgm",
        "music",
        "sound",
    ),
}

# Note: "音" alone is too generic (matches many compounds); handle separately
# via substring but prefer longer keywords first.

_LIKE_TOKENS: tuple[str, ...] = (
    "良い",
    "よい",
    "好き",
    "いい",
    "素晴らしい",
    "最高",
    "good",
    "like",
    "liked",
    "likes",
    "great",
    "excellent",
    "nice",
    "love",
    "loved",
    "loves",
    "awesome",
    "perfect",
    "fantastic",
)

_DISLIKE_TOKENS: tuple[str, ...] = (
    "悪い",
    "わるい",
    "嫌い",
    "ひどい",
    "ダメ",
    "最悪",
    "bad",
    "dislike",
    "disliked",
    "hate",
    "hated",
    "terrible",
    "poor",
    "awful",
    "horrible",
    "worst",
)

_AMBIGUOUS_PHRASES: tuple[str, ...] = (
    "全部良い",
    "全部いい",
    "全部よい",
    "全て良い",
    "すべて良い",
    "すべてよい",
    "全部良いです",
    "everything is good",
    "everything good",
    "everything is great",
    "all good",
    "all is good",
    "all good!",
)


def _contains_token(text: str, tokens: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(tok.lower() in low for tok in tokens)


def _segment_polarity(segment: str) -> Polarity | None:
    has_like = _contains_token(segment, _LIKE_TOKENS)
    has_dislike = _contains_token(segment, _DISLIKE_TOKENS)
    if has_like and has_dislike:
        return "neutral"
    if has_like:
        return "like"
    if has_dislike:
        return "dislike"
    return None


def _detect_domains_in_text(text: str) -> list[PreferenceDomain]:
    low = text.lower()
    found: list[PreferenceDomain] = []
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in low:
                found.append(domain)
                break
        if domain == PreferenceDomain.audio and domain not in found and "音" in text:
            found.append(domain)
    return found


def _split_segments(comment: str) -> list[str]:
    # Split on common delimiters that separate domain statements
    # Keep segments non-empty. Delimiters: slash etc and also and/but
    # Use regex to split; also handle Japanese punctuation
    parts = re.split(r"[／/，,、。;；\n]+", comment)  # noqa: RUF001
    # Further split on " and " / " but " boundaries inside parts
    refined: list[str] = []
    for p in parts:
        # split on english conjunctions that often separate clauses
        sub = re.split(r"\s+and\s+|\s+but\s+|\s+&amp;\s+", p, flags=re.IGNORECASE)
        for raw in sub:
            seg = raw.strip()
            if seg:
                refined.append(seg)
    if not refined:
        return [comment.strip()] if comment.strip() else []
    return refined


def extract_domains_seeded(comment: str) -> ParsedAnnotationDraft:  # noqa: C901, PLR0912
    """Deterministic keyword/pattern extraction.

    Treats ``comment`` as pure DATA — never executes instructions inside it.
    """
    if not isinstance(comment, str):
        raise TypeError("comment must be str")
    stripped = comment.strip()
    if not stripped:
        return ParsedAnnotationDraft(
            named_domains=(),
            domains={},
            needs_review=True,
            rationale=comment or "empty comment",
            confidence=0.5,
        )
    low = stripped.lower()

    # Ambiguous trap — must be checked before domain detection
    for phrase in _AMBIGUOUS_PHRASES:
        if phrase.lower() in low:
            return ParsedAnnotationDraft(
                named_domains=(),
                domains={},
                needs_review=True,
                rationale=stripped,
                confidence=0.4,
            )
    # Also handle bare "全部" / "everything" without explicit domain keywords
    # If comment is short and contains "全部" or "everything" / "all" plus a like token
    # but no specific domain keyword, treat as ambiguous.
    has_like_global = _contains_token(stripped, _LIKE_TOKENS)
    domain_candidates_global = _detect_domains_in_text(stripped)
    if not domain_candidates_global and has_like_global:
        # Check for vague quantifiers
        vague = ("全部", "全て", "すべて", "everything", "all")
        if any(v.lower() in low for v in vague):
            return ParsedAnnotationDraft(
                named_domains=(),
                domains={},
                needs_review=True,
                rationale=stripped,
                confidence=0.4,
            )

    segments = _split_segments(stripped)
    # Map domain -> polarity via segment that contains the domain keyword
    domain_to_polarity: dict[PreferenceDomain, Polarity] = {}
    detected_domains: list[PreferenceDomain] = []

    # First, find all domains present anywhere
    all_detected = _detect_domains_in_text(stripped)
    if not all_detected:
        # No domain keywords → ambiguous / unspecified
        return ParsedAnnotationDraft(
            named_domains=(),
            domains={},
            needs_review=True,
            rationale=stripped,
            confidence=0.45,
        )

    for domain in all_detected:
        # Find the segment(s) that contain this domain's keyword
        kw_list = _DOMAIN_KEYWORDS[domain]
        # special handling for audio single char
        best_polarity: Polarity | None = None
        for seg in segments:
            seg_low = seg.lower()
            contains = any(kw.lower() in seg_low for kw in kw_list)
            if domain == PreferenceDomain.audio and "音" in seg and not contains:
                contains = True
            if contains:
                pol = _segment_polarity(seg)
                if pol is not None:
                    best_polarity = pol
                    break
        if best_polarity is None:
            # Fallback: look at whole comment polarity if segment had no token
            # But for mixed cases this would conflate; only use if whole comment
            # has однознач polarity
            global_pol = _segment_polarity(stripped)
            if global_pol is not None and global_pol != "neutral":
                best_polarity = global_pol
            else:
                best_polarity = "like"
        domain_to_polarity[domain] = best_polarity
        detected_domains.append(domain)

    # Deduplicate while preserving PreferenceDomain enum order for determinism
    # Use canonical enum order to keep output stable
    canonical_order = list(PreferenceDomain)
    named = tuple(sorted(set(detected_domains), key=lambda d: canonical_order.index(d)))  # noqa: PLW0108

    # Build domains dict for named only
    domains: dict[PreferenceDomain, Polarity] = {d: domain_to_polarity[d] for d in named}

    return ParsedAnnotationDraft(
        named_domains=named,
        domains=domains,
        needs_review=False,
        rationale=stripped,
        confidence=0.85,
    )


def extract_domains_llm(
    comment: str,
    llm_call: Callable[[str], Any],
) -> ParsedAnnotationDraft:
    """LLM seam — injectable callable, schema-validated post-hoc.

    ``llm_call`` is called with the raw comment string and must return either
    a ``ParsedAnnotationDraft`` instance or a dict that can be validated into
    one. Unknown domain values are rejected via schema validation. The comment
    itself is never executed.
    """
    if not callable(llm_call):
        raise TypeError("llm_call must be callable")
    raw = llm_call(comment)
    if isinstance(raw, ParsedAnnotationDraft):
        # Re-validate via JSON round-trip to catch bypassed construction
        return ParsedAnnotationDraft.model_validate(raw.model_dump(mode="json"))
    if isinstance(raw, dict):
        return ParsedAnnotationDraft.model_validate(raw)
    raise TypeError("llm_call must return ParsedAnnotationDraft or dict")


__all__ = [
    "ParsedAnnotationDraft",
    "extract_domains_llm",
    "extract_domains_seeded",
]
