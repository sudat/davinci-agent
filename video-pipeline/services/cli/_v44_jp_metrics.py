"""Japanese evidence metrics binding for the T12 subtitle proof harness.

Thin alias over Task 5's canonical JP metric module
(``services.metrics.v44_product_proof``), which landed while T12 was in
flight. This module is the SINGLE import site so the binding is one-file
adaptation:

- the corrected sample loads through T5's ``TranscriptSampleV1``
  (schema ``v44-transcript-sample-v1``; segments + proper-noun id->surface
  mapping) with T12-side fail-closed checks (missing/corrupt file, and a
  sample that carries no segments/proper nouns is refused — this harness
  needs every evidence input present, no nullable shortcuts);
- CER / proper-noun recall / p95 / omitted / duplicated all come from T5's
  ``compute_evidence_quality`` — never reimplemented here;
- the harness owns ONLY what T5 deliberately leaves to the caller: the
  hypothesis<->reference utterance pairing that produces timestamp diffs,
  and the dictionary-aware hypothesis proper-noun surface mapping (the
  channel+episode dictionary is the pipeline's deterministic post-ASR
  proper-noun correction, so the surface the system would render for a term
  is its canonical form whenever the raw text contains the canonical or any
  variant; otherwise the term is simply absent from the transcript).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.metrics.v44_product_proof import (
    EvidenceQualityMetrics,
    TranscriptSampleV1,
    TranscriptSegment,
    compute_evidence_quality,
)

if TYPE_CHECKING:
    from services.creative_plan.subtitle_text import ProperNounDictionaryV1

T5_SOURCE = "services.metrics.v44_product_proof"

# Normalization mirrors services.toolchain.whisper_ja._normalize_japanese:
# punctuation, brackets, and whitespace never count as transcript content.
_STRIP_RE = re.compile(r"[、。.,!?\s‥…「」『』・\u3000]")
_SIMILARITY_THRESHOLD = 0.5


class JpMetricsError(Exception):
    """Typed refusal from the JP metrics alias; carries a machine label."""

    def __init__(self, label: str, detail: str) -> None:
        super().__init__(f"{label}: {detail}")
        self.label = label
        self.detail = detail


def load_transcript_sample(path: Path) -> tuple[TranscriptSampleV1, str]:
    """Load T5's corrected sample; returns it with the file's sha256.

    Fail-closed: missing file, corrupt JSON, and a sample without segments
    or without proper nouns are distinct typed errors — this harness never
    runs with nullable evidence inputs.
    """
    if not path.is_file():
        raise JpMetricsError("sample-missing", f"corrected sample not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise JpMetricsError("sample-unreadable", f"{path}: {error}") from error
    try:
        sample = TranscriptSampleV1.model_validate_json(raw)
    except ValidationError as error:
        raise JpMetricsError(
            "sample-parse-error", f"corrected sample invalid at {path}: {error}"
        ) from error
    if not sample.segments:
        raise JpMetricsError(
            "sample-parse-error", f"corrected sample carries no segments: {path}"
        )
    if not sample.proper_nouns:
        raise JpMetricsError(
            "sample-parse-error", f"corrected sample carries no proper nouns: {path}"
        )
    return sample, sha256_file(path)


def _normalize(text: str) -> str:
    return _STRIP_RE.sub("", text)


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for index_a, char_a in enumerate(a, start=1):
        current = [index_a]
        for index_b, char_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[index_b] + 1,
                    current[index_b - 1] + 1,
                    previous[index_b - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


def utterance_diffs_ms(
    hypothesis: Sequence[TranscriptSegment], reference: Sequence[TranscriptSegment]
) -> tuple[int, ...]:
    """Greedy one-to-one pairing over text similarity; start-time deltas.

    T5's ``timestamp_error_p95_ms`` consumes these diffs (matching policy is
    deliberately the caller's). Pairs below the similarity threshold never
    match; deterministic tie-break by (similarity desc, indices).
    """
    pairs = sorted(
        (
            (_similarity(_normalize(ref.text), _normalize(hyp.text)), i, j)
            for i, ref in enumerate(reference)
            for j, hyp in enumerate(hypothesis)
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    taken_ref: set[int] = set()
    taken_hyp: set[int] = set()
    diffs: list[int] = []
    for similarity, i, j in pairs:
        if i in taken_ref or j in taken_hyp or similarity < _SIMILARITY_THRESHOLD:
            continue
        taken_ref.add(i)
        taken_hyp.add(j)
        diffs.append(abs(hypothesis[j].start_ms - reference[i].start_ms))
    return tuple(diffs)


def hypothesis_proper_nouns(
    hypothesis_text: str,
    sample: TranscriptSampleV1,
    dictionary: ProperNounDictionaryV1,
) -> dict[str, str]:
    """Surface the system transcript renders for each sample proper-noun id.

    For a term whose expected surface is a dictionary canonical (or appears
    in that entry's variants): the canonical is rendered iff the raw text
    contains the canonical or any variant. Terms unknown to the dictionary
    count as rendered only when the expected surface itself appears verbatim
    in the raw text. Absent terms map to "" (never fabricated).
    """
    rendered: dict[str, str] = {}
    for term_id, expected in sample.proper_nouns.items():
        entry = next(
            (e for e in dictionary.entries if e.canonical == expected or expected in e.variants),
            None,
        )
        if entry is None:
            rendered[term_id] = expected if expected in hypothesis_text else ""
            continue
        forms = (entry.canonical, *entry.variants)
        hit = any(form in hypothesis_text for form in forms)
        rendered[term_id] = entry.canonical if hit else ""
    return rendered


def evidence_quality(
    artifact_segments: Sequence[TranscriptSegment],
    sample: TranscriptSampleV1,
    dictionary: ProperNounDictionaryV1,
) -> EvidenceQualityMetrics:
    """Compose the evidence block via T5's canonical ``compute_evidence_quality``."""
    raw_text = "".join(segment.text for segment in artifact_segments)
    return compute_evidence_quality(
        corrected=sample,
        hypothesis_segments=tuple(artifact_segments),
        hypothesis_proper_nouns=hypothesis_proper_nouns(raw_text, sample, dictionary),
        timestamp_diffs_ms=[
            float(diff) for diff in utterance_diffs_ms(artifact_segments, sample.segments)
        ],
    )


__all__ = [
    "T5_SOURCE",
    "JpMetricsError",
    "TranscriptSampleV1",
    "TranscriptSegment",
    "evidence_quality",
    "load_transcript_sample",
]
