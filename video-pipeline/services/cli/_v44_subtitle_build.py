"""Pure proof-build steps for the T12 subtitle proof harness.

Transcript artifact -> subtitle plan -> cues -> external SRT snippet ->
subtitle-side QC + evidence-side metrics. No CLI, no argument parsing:
``services.cli.v44_subtitle_proof`` orchestrates these; everything here is
deterministic over its inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.cli._v44_jp_metrics import (
    TranscriptSampleV1,
    TranscriptSegment,
    evidence_quality,
)
from services.cli._v44_subtitle_report import (
    EvidenceBlock,
    SubtitleBlock,
    subtitle_proof_policy,
)
from services.conform.rate_model import round_half_away
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.compile_ir_v2 import SourceFactsV2, SourceFactV2
from services.creative_plan.ir_models_v2 import PlacedClipV2, TimelineIrV2, VideoTrackV2
from services.creative_plan.subtitle_models import (
    AsrSegmentV1,
    SubtitleBuildOptions,
    SubtitlePlanV1,
)
from services.creative_plan.subtitle_plan import build_subtitle_plan
from services.creative_plan.subtitle_text import (
    ProperNounDictionaryV1,
    ProperNounEntryV1,
    load_proper_nouns,
)
from services.foundation_io import atomic_write, sha256_file
from services.preview.srt import SubtitleCue, parse_srt, render_srt
from services.qc.checks.subtitle_checks import evaluate_cues
from services.qc.issue_factory import IssueFactory

if TYPE_CHECKING:
    from services.analyze.asr_models import TranscriptArtifact

RATE = RationalFrameRate(num=30, den=1)
SOURCE_ID = "src-v44-proof"
SNIPPET_NAME = "preview-snippet.srt"


class SubtitleProofError(Exception):
    """Typed refusal naming the missing/invalid input; never a partial run."""

    def __init__(self, label: str, detail: str) -> None:
        super().__init__(f"{label}: {detail}")
        self.label = label
        self.detail = detail


def merged_proper_nouns(episode_path: Path | None) -> ProperNounDictionaryV1:
    """Channel dictionary + episode-local additions merged at this call site.

    Episode-local entries append their canonicals; a colliding canonical
    merges variants (channel variants first, episode additions after, dedup).
    """
    channel = load_proper_nouns()
    if episode_path is None:
        return channel
    episode = load_proper_nouns(episode_path)
    entries = {entry.canonical: entry for entry in channel.entries}
    for entry in episode.entries:
        existing = entries.get(entry.canonical)
        variants = (
            entry.variants
            if existing is None
            else tuple(dict.fromkeys((*existing.variants, *entry.variants)))
        )
        entries[entry.canonical] = ProperNounEntryV1(
            canonical=entry.canonical, variants=variants
        )
    return ProperNounDictionaryV1(
        schema_version="proper-nouns-ja-v1", entries=tuple(entries.values())
    )


def forward_segments(
    artifact: TranscriptArtifact,
) -> tuple[tuple[TranscriptSegment, ...], int]:
    """Forward-span T5 segments (whisper may emit zero-length ones)."""
    segments: list[TranscriptSegment] = []
    skipped = 0
    for segment in artifact.segments:
        if segment.end_ms <= segment.start_ms:
            skipped += 1
            continue
        segments.append(
            TranscriptSegment(
                start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text
            )
        )
    if not segments:
        raise SubtitleProofError(
            "asr-no-forward-segments", "the transcript artifact carries no forward segment"
        )
    return tuple(segments), skipped


def build_plan(
    segments: Sequence[TranscriptSegment],
    *,
    episode_id: str,
    dictionary: ProperNounDictionaryV1,
) -> SubtitlePlanV1:
    """Identity-edit subtitle plan: one placement spanning the whole source.

    The proof harness places every ASR segment (identity edit) so the plan
    ladder (timing, text rules, reconciliation, chunking, reading speed,
    capability ordering) runs exactly as it would on a real edit.
    """
    asr = tuple(
        AsrSegmentV1(
            segment_id=f"asr-{index:03d}",
            source_id=SOURCE_ID,
            text=segment.text,
            start_seconds=segment.start_ms / 1000,
            end_seconds=segment.end_ms / 1000,
        )
        for index, segment in enumerate(segments)
    )
    rate_fraction = RATE.as_fraction
    duration = max(
        round_half_away(Fraction(segment.end_seconds) * rate_fraction) for segment in asr
    )
    span = SourceFrameSpan(start_frame=0, end_frame=duration, rate=RATE)
    ir = TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id=episode_id,
        rate=RATE,
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="v-primary",
                items=(
                    PlacedClipV2(
                        item_id="itm-proof-0",
                        source=SourceRef(source_id=SOURCE_ID, span=span),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=duration),
                        candidate_ref="cand-proof-0",
                    ),
                ),
            ),
        ),
    )
    return build_subtitle_plan(
        asr,
        source_facts=SourceFactsV2(
            rate=RATE, sources=(SourceFactV2(source_id=SOURCE_ID, duration_frames=duration),)
        ),
        ir_v2=ir,
        options=SubtitleBuildOptions(proper_nouns=dictionary),
    )


def subtitle_side(
    plan: SubtitlePlanV1, out_dir: Path, evidence_inputs: tuple[str, ...]
) -> SubtitleBlock:
    """Render + verify the external SRT snippet, run evaluate_cues, report.

    Layout-quality results ONLY (PRD §7.4): nothing here reads the corrected
    sample or any transcript-quality metric.
    """
    cues = tuple(
        SubtitleCue(
            start_ms=_frame_ms(cue.record_span.start_frame),
            end_ms=_frame_ms(cue.record_span.end_frame),
            text="\n".join(cue.lines),
        )
        for cue in plan.cues
    )
    snippet_bytes = render_srt(cues)
    if parse_srt(snippet_bytes) != cues:
        raise SubtitleProofError(
            "snippet-roundtrip-mismatch", "rendered SRT does not parse back to the plan cues"
        )
    policy = subtitle_proof_policy(
        max_lines=plan.style_profile.lines_per_cue,
        max_chars_per_line=plan.style_profile.chars_per_line,
    )
    issues = evaluate_cues(cues, policy, IssueFactory.for_policy(policy, evidence_inputs))
    snippet_path = out_dir / SNIPPET_NAME
    atomic_write(snippet_path, snippet_bytes)

    lines_per_cue = [cue.text.count("\n") + 1 for cue in cues]
    chars_per_line = [len(line) for cue in cues for line in cue.text.split("\n")]
    profile = plan.style_profile
    within = (
        max(lines_per_cue) <= profile.lines_per_cue
        and max(chars_per_line) <= profile.chars_per_line
    )
    return SubtitleBlock(
        cue_count=len(cues),
        matrix_status=plan.capability_path.matrix_status,
        capability_path=plan.capability_path.selected,
        snippet_render="external_srt",
        snippet_path=str(snippet_path),
        snippet_sha256=sha256_file(snippet_path),
        qc_issue_count=len(issues),
        qc_rule_ids=tuple(sorted({issue.rule_id for issue in issues})),
        reading_speed_violations=len(plan.violations),
        legibility_note=(
            f"{len(cues)} cues; max {max(lines_per_cue)} line(s)/cue and "
            f"{max(chars_per_line)} chars/line vs profile {profile.chars_per_line}"
            f"x{profile.lines_per_cue}; {'within' if within else 'exceeds'} limits"
        ),
    )


def _frame_ms(frames: int) -> int:
    # The QC/package millisecond rounding (subtitle_checks._span_ms convention).
    return (frames * 1000 * RATE.den + RATE.num // 2) // RATE.num


def evidence_block(
    segments: Sequence[TranscriptSegment],
    sample: TranscriptSampleV1,
    dictionary: ProperNounDictionaryV1,
) -> EvidenceBlock:
    """Transcript-quality metrics ONLY (PRD §7.4), via T5's canonical module."""
    quality = evidence_quality(segments, sample, dictionary)
    cer = quality.transcript_cer
    recall = quality.proper_noun_recall
    omitted = quality.omitted_utterances
    duplicated = quality.duplicated_utterances
    if cer is None or recall is None or omitted is None or duplicated is None:
        raise SubtitleProofError(
            "evidence-missing-inputs",
            "a required evidence metric came back null (missing inputs); refusing",
        )
    return EvidenceBlock(
        metrics_source="services.metrics.v44_product_proof",
        transcript_cer=cer,
        proper_noun_recall=recall,
        timestamp_error_p95_ms=quality.timestamp_error_p95_ms,
        omitted_utterances=omitted,
        duplicated_utterances=duplicated,
    )


__all__ = [
    "RATE",
    "SubtitleProofError",
    "build_plan",
    "evidence_block",
    "forward_segments",
    "merged_proper_nouns",
    "subtitle_side",
]
