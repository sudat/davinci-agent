"""Episode proper-noun substitution for the V44-0 arm transcript path.

Round-3 NO-GO correction B (PRD §6.7 bounded correction): the real whisper
hypothesis misheard the operator-confirmed proper nouns ("DJIポケット4",
bare "PS"), feeding wrong evidence into selection (round-1 proper_noun_recall
0.0). This module applies the SAME channel+episode dictionary merge the
subtitle harness uses (``_v44_subtitle_build.merged_proper_nouns``) to the
ASR text BEFORE the MI build, so shot descriptions, MI transcript segments,
and the evidence-quality hypothesis all carry the canonical terms. Post-ASR
text substitution only — the ASR run and its artifacts are never edited.
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


def substituted_arm_transcript(
    speech: tuple[SpeechSegment, ...],
    hypothesis_ms: tuple[tuple[int, int, str], ...],
    episode_root: Path,
) -> tuple[tuple[SpeechSegment, ...], tuple[tuple[int, int, str], ...], int]:
    """Apply the dictionary to the ASR speech texts and the ms hypothesis.

    Returns the substituted speech tuple (feeds the MI artifact), the
    substituted hypothesis triples (feed evidence-quality metrics — the
    measurement targets the pipeline's transcript path, matching what the
    director consumed), and the number of substitutions applied.
    """

    dictionary = arm_proper_noun_dictionary(episode_root)
    out: list[SpeechSegment] = []
    applied = 0
    for segment in speech:
        text, substitutions, _spans = apply_proper_nouns(segment.text, dictionary)
        applied += len(substitutions)
        out.append(replace(segment, text=text))
    hypothesis = tuple(
        (start, end, apply_proper_nouns(text, dictionary)[0])
        for start, end, text in hypothesis_ms
    )
    return tuple(out), hypothesis, applied


__all__ = [
    "arm_proper_noun_dictionary",
    "substituted_arm_transcript",
]
