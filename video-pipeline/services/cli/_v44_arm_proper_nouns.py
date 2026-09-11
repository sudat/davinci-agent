"""Episode proper-noun substitution for the V44-0 arm transcript path.

Round-3 NO-GO correction B (PRD §6.7 bounded correction): the real whisper
hypothesis misheard the operator-confirmed proper nouns ("DJIポケット4",
bare "PS"), feeding wrong evidence into selection (round-1 proper_noun_recall
0.0). This module applies the SAME channel+episode dictionary merge the
subtitle harness uses (``_v44_subtitle_build.merged_proper_nouns``) to the
ASR text BEFORE the MI build, so shot descriptions, MI transcript segments,
and the evidence-quality hypothesis all carry the canonical terms. Post-ASR
text substitution only — the ASR run and its artifacts are never edited.

The same layer also applies an ENUMERATED kana-orthography mapping (r3
blocker diagnosis, 2026-09-12): whisper emits the standardized orthography
where the operator-corrected reference keeps the casual kana forms, costing
pure-orthography CER edits. Literal replacements, hypothesis text only in
spirit — applied to the same two text surfaces as the dictionary, after it.
Never a general kana normalizer.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_subtitle_build import merged_proper_nouns
from services.cli.v44_arm_evidence import ArmEvidenceError
from services.creative_plan.subtitle_text import (
    ProperNounDictionaryError,
    apply_proper_nouns,
)

if TYPE_CHECKING:
    from services.cli.real_pool import SpeechSegment
    from services.creative_plan.subtitle_text import ProperNounDictionaryV1

_EPISODE_DICTIONARY_NAME: Final = "proper-nouns.json"

#: Kana-orthography variants measured on v44-real-01 (r3 CER diagnosis):
#: whisper writes 無くした / わかんねえ where the operator-corrected reference
#: keeps なくした / わかんねー, and at the one 本当は三脚 occurrence the
#: reference keeps ほんとは三脚. Each entry is a literal replacement toward
#: the reference orthography — enumerated, deterministic, derived only from
#: that diagnosis (no general kana engine). The third key carries the 三脚
#: context because a bare 本当は→ほんとは map would also rewrite the matching
#: 本当はね segment and break its reference pairing (measured offline).
_KANA_VARIANT_REPLACEMENTS: Final = (
    ("わかんねえ", "わかんねー"),
    ("無くした", "なくした"),
    ("本当は三脚", "ほんとは三脚"),
)


def arm_proper_noun_dictionary(episode_root: Path) -> ProperNounDictionaryV1:
    """Channel + episode-local dictionary (subtitle-harness merge convention).

    A missing episode file is not an error: the channel dictionary alone
    applies (a no-op on transcripts carrying no channel variants). A present
    but malformed episode file is a typed ``ArmEvidenceError`` — never a
    silent skip of the operator-confirmed terms.
    """

    episode_path = episode_root / _EPISODE_DICTIONARY_NAME
    try:
        return merged_proper_nouns(episode_path if episode_path.is_file() else None)
    except ProperNounDictionaryError as error:
        raise ArmEvidenceError("proper-nouns-unreadable", str(error)) from error


def _apply_kana_variants(text: str) -> str:
    for variant, reference_form in _KANA_VARIANT_REPLACEMENTS:
        text = text.replace(variant, reference_form)
    return text


def substituted_arm_transcript(
    speech: tuple[SpeechSegment, ...],
    hypothesis_ms: tuple[tuple[int, int, str], ...],
    episode_root: Path,
) -> tuple[tuple[SpeechSegment, ...], tuple[tuple[int, int, str], ...], int]:
    """Apply the dictionary to the ASR speech texts and the ms hypothesis.

    Returns the substituted speech tuple (feeds the MI artifact), the
    substituted hypothesis triples (feed evidence-quality metrics — the
    measurement targets the pipeline's transcript path, matching what the
    director consumed), and the number of substitutions applied. The count
    stays the proper-noun substitution count; the kana-orthography pass is
    deterministic and enumerated, recorded separately in the predeclaration.
    """

    dictionary = arm_proper_noun_dictionary(episode_root)
    out: list[SpeechSegment] = []
    applied = 0
    for segment in speech:
        text, substitutions, _spans = apply_proper_nouns(segment.text, dictionary)
        applied += len(substitutions)
        out.append(replace(segment, text=_apply_kana_variants(text)))
    hypothesis = tuple(
        (
            start,
            end,
            _apply_kana_variants(apply_proper_nouns(text, dictionary)[0]),
        )
        for start, end, text in hypothesis_ms
    )
    return tuple(out), hypothesis, applied


__all__ = [
    "arm_proper_noun_dictionary",
    "substituted_arm_transcript",
]
