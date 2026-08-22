"""Japanese subtitle text rules (task 33; PRD §9.2) — deterministic, no LLM.

Rule tables (the documented contract; task 60 preview and task 41 QC consume
their outputs):

Punctuation normalization (``PUNCT_NORMALIZATION``): half-width ASCII
``! ? , .`` map to their full-width Japanese forms, ``‥`` collapses to ``…``,
and ASCII spaces are removed (Japanese subtitle text carries no inter-word
spaces; dictionary variants are written against the NORMALIZED text).

Fillers (``FILLER_TOKENS_JA``): common fillers えー/えーと/えっと/あの/あのー/
まあ/そのー/ええ/うん. Policy is explicit and never silent: ``remove`` strips
the token plus one immediately trailing ``、``/``。`` and reports the removed
terms; ``retain`` leaves the text untouched.

Semantic line breaking — bunsetsu-style break opportunities, deterministic:
- ``BOUNDARY_TOKENS_JA`` — a break is allowed AFTER these (longest match
  first): clause punctuation 、。 plus the full-width exclamation, question,
  question-ellipsis and middle-dot marks, and case particles/clause ends
  ``は が を に へ で と も の から まで より ね よ か な``.
- ``NO_LINE_START_CHARS`` — a line never STARTS with these: small kana
  ``ゃゅょぁぃぅぇぉっ``, long-vowel ``ー``, closing punctuation
  and brackets (no orphaned punctuation or dangling小書き仮名).
- ``NO_LINE_END_CHARS`` — a line never ENDS with these: small kana and ``ー``
  (extension marks attach to the following char).
- Atomic spans (proper-noun dictionary terms) — never broken strictly inside;
  a break AT the span edge is legal.
- ``break_into_lines`` wraps greedily: the LAST rule boundary inside the
  chars-per-line window wins; a window with no boundary hard-breaks at the
  window edge, shifted left past every prohibited position. If the whole
  window sits inside an atomic span (dictionary term wider than the line),
  the line overflows to the atomic edge instead — atomic units are never
  split, even past the width limit. Every character counts as one column
  (no wcwidth weighting).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel

FillerPolicy = Literal["retain", "remove"]


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value

PUNCT_NORMALIZATION: Final[dict[str, str]] = {
    ",": "、",
    ".": "。",
    "!": "！",  # noqa: RUF001 (Japanese punctuation is the rule data)
    "?": "？",  # noqa: RUF001 (Japanese punctuation is the rule data)
    "‥": "…",
    " ": "",
}

FILLER_TOKENS_JA: Final[frozenset[str]] = frozenset(
    {"えー", "えーと", "えっと", "あの", "あのー", "まあ", "そのー", "ええ", "うん"}
)
_FILLERS_LONGEST_FIRST: Final[tuple[str, ...]] = tuple(
    sorted(FILLER_TOKENS_JA, key=lambda token: (-len(token), token))
)

BOUNDARY_TOKENS_JA: Final[tuple[str, ...]] = tuple(
    sorted(
        {*"、。！？…・", *("は", "が", "を", "に", "へ", "で", "と", "も", "の", "ね", "よ", "か", "な"), "から", "まで", "より"},  # noqa: RUF001, E501
        key=lambda token: (-len(token), token),
    )
)
NO_LINE_START_CHARS: Final[frozenset[str]] = frozenset(
    "ゃゅょぁぃぅぇぉっー、。！？…‥・」』）"  # noqa: RUF001 (frozen JA rule table)
)
NO_LINE_END_CHARS: Final[frozenset[str]] = frozenset("ゃゅょぁぃぅぇぉっー")


def normalize_punctuation(text: str) -> str:
    """Apply the punctuation/space mapping table char by char."""
    return "".join(PUNCT_NORMALIZATION.get(char, char) for char in text)


def punctuation_changes(text: str) -> tuple[tuple[str, str], ...]:
    """Which mapping pairs the table would apply to *text* (for provenance)."""
    return tuple(
        (raw, mapped)
        for raw, mapped in sorted(PUNCT_NORMALIZATION.items())
        if raw in text and mapped != raw
    )


def apply_filler_policy(text: str, policy: FillerPolicy) -> tuple[str, tuple[str, ...]]:
    """Strip (or keep) fillers; returns text plus the removed terms, sorted."""
    if policy == "retain":
        return text, ()
    kept: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(text):
        token = next((t for t in _FILLERS_LONGEST_FIRST if text.startswith(t, index)), None)
        if token is None:
            kept.append(text[index])
            index += 1
            continue
        removed.append(token)
        index += len(token)
        if index < len(text) and text[index] in "、。":
            index += 1
    return "".join(kept), tuple(sorted(removed))


def present_fillers(text: str) -> tuple[str, ...]:
    """Fillers occurring anywhere in *text* (for the explicit retain note)."""
    return tuple(sorted(token for token in FILLER_TOKENS_JA if token in text))


class ProperNounEntryV1(StrictModel):
    """One canonical channel term plus the ASR variants that map onto it."""

    canonical: Annotated[str, Field(min_length=1, strict=True)]
    variants: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)


class ProperNounDictionaryV1(StrictModel):
    """Channel profile proper-noun dictionary (config/subtitles)."""

    schema_version: Literal["proper-nouns-ja-v1"]
    entries: Annotated[tuple[ProperNounEntryV1, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def require_unique_canonicals(self) -> ProperNounDictionaryV1:
        canonicals = [entry.canonical for entry in self.entries]
        if len(set(canonicals)) != len(canonicals):
            raise PydanticCustomError("duplicate_canonical", "canonical terms are unique")
        return self


class ProperNounSubstitutionV1(StrictModel):
    """One applied dictionary substitution (post-ASR, no ASR re-run)."""

    variant: Annotated[str, Field(min_length=1, strict=True)]
    canonical: Annotated[str, Field(min_length=1, strict=True)]


class ProperNounDictionaryError(ValueError):
    """Typed refusal while loading the proper-noun dictionary file."""


_DEFAULT_DICTIONARY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "subtitles" / "proper-nouns-ja.json"
)


def load_proper_nouns(path: Path | None = None) -> ProperNounDictionaryV1:
    """Load and validate the channel dictionary (default: config/subtitles)."""
    resolved = path if path is not None else _DEFAULT_DICTIONARY_PATH
    try:
        return ProperNounDictionaryV1.model_validate_json(resolved.read_bytes())
    except (OSError, ValidationError) as error:
        detail = f"cannot load proper-noun dictionary {resolved}: {error}"
        raise ProperNounDictionaryError(detail) from error


def apply_proper_nouns(
    text: str, dictionary: ProperNounDictionaryV1
) -> tuple[str, tuple[ProperNounSubstitutionV1, ...], tuple[tuple[int, int], ...]]:
    """Substitute variants with canonical terms, longest variant first.

    Returns the substituted text, the substitutions (provenance), and the
    canonical-term spans as half-open char offsets — atomic units the line
    breaker must never split.
    """
    pairs = sorted(
        ((variant, entry.canonical) for entry in dictionary.entries for variant in entry.variants),
        key=lambda pair: (-len(pair[0]), pair[0], pair[1]),
    )
    out: list[str] = []
    substitutions: list[ProperNounSubstitutionV1] = []
    spans: list[tuple[int, int]] = []
    out_length = 0
    index = 0
    while index < len(text):
        pair = next(((v, c) for v, c in pairs if text.startswith(v, index)), None)
        if pair is None:
            out.append(text[index])
            out_length += 1
            index += 1
            continue
        variant, canonical = pair
        out.append(canonical)
        substitutions.append(ProperNounSubstitutionV1(variant=variant, canonical=canonical))
        spans.append((out_length, out_length + len(canonical)))
        out_length += len(canonical)
        index += len(variant)
    return "".join(out), tuple(substitutions), tuple(spans)


def allowed_break_offsets(
    text: str, atomic_spans: tuple[tuple[int, int], ...] = ()
) -> frozenset[int]:
    """Char offsets where a line break is legal (break BEFORE text[offset])."""
    offsets: set[int] = set()
    index = 0
    while index < len(text):
        token = next((t for t in BOUNDARY_TOKENS_JA if text.startswith(t, index)), None)
        if token is None:
            index += 1
            continue
        offset = index + len(token)
        legal_start = offset < len(text) and text[offset] not in NO_LINE_START_CHARS
        if legal_start and text[offset - 1] not in NO_LINE_END_CHARS:
            offsets.add(offset)
        index += len(token)
    for start, end in atomic_spans:
        offsets = {offset for offset in offsets if not start < offset < end}
    return frozenset(offsets)


def _bad_break(text: str, offset: int, atomic_spans: tuple[tuple[int, int], ...]) -> bool:
    if text[offset] in NO_LINE_START_CHARS or text[offset - 1] in NO_LINE_END_CHARS:
        return True
    return any(start < offset < end for start, end in atomic_spans)


def break_into_lines(
    text: str, chars_per_line: int, atomic_spans: tuple[tuple[int, int], ...] = ()
) -> tuple[str, ...]:
    """Wrap *text* into lines of at most *chars_per_line* columns."""
    if chars_per_line <= 0:
        raise ValueError("chars_per_line must be positive")
    allowed = allowed_break_offsets(text, atomic_spans)
    lines: list[str] = []
    start = 0
    while start < len(text):
        limit = start + chars_per_line
        if len(text) <= limit:
            lines.append(text[start:])
            break
        window = {offset for offset in allowed if start < offset <= limit}
        if window:
            cut = max(window)
        else:
            cut = limit
            while cut > start + 1 and _bad_break(text, cut, atomic_spans):
                cut -= 1
            if _bad_break(text, cut, atomic_spans):
                cut = next(
                    (
                        offset
                        for offset in range(limit + 1, len(text))
                        if not _bad_break(text, offset, atomic_spans)
                    ),
                    len(text),
                )
        lines.append(text[start:cut])
        start = cut
    return tuple(lines)


__all__ = [
    "BOUNDARY_TOKENS_JA",
    "FILLER_TOKENS_JA",
    "NO_LINE_END_CHARS",
    "NO_LINE_START_CHARS",
    "PUNCT_NORMALIZATION",
    "FillerPolicy",
    "ProperNounDictionaryError",
    "ProperNounDictionaryV1",
    "ProperNounEntryV1",
    "ProperNounSubstitutionV1",
    "allowed_break_offsets",
    "apply_filler_policy",
    "apply_proper_nouns",
    "break_into_lines",
    "load_proper_nouns",
    "normalize_punctuation",
    "present_fillers",
    "punctuation_changes",
]
