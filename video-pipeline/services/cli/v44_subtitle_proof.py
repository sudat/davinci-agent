"""``python -m services.cli.v44_subtitle_proof`` — Japanese subtitle live-proof harness (T12).

End-to-end proof runner for Japanese speech material:

1. REAL ASR — the pinned whisper.cpp CLI through
   ``services.analyze.asr_whisper_cpp.transcribe`` (frozen argv, hash-bound
   pins; never a synthetic transcript). ``--asr-artifact`` replays a
   recorded transcript artifact for offline Tier B reproduction (bytes
   hash-noted in the report).
2. Subtitle plan via ``build_subtitle_plan`` with the channel proper-noun
   dictionary (``config/subtitles/proper-nouns-ja.json``) MERGED with an
   episode-local additions file (``--proper-nouns``) at the call site —
   the dictionary schema itself is untouched.
3. A subtitle-bearing preview snippet rendered through the external ASS/SRT
   rung (the mcp-fit subtitle row is probe-level "accepted" with generation
   itself not exercised, so the external fallback renders the snippet —
   never blocked on MCP-native generation).
4. Evidence side (transcript quality vs the corrected sample) and subtitle
   side (cue layout quality) are computed SEPARATELY into a
   ``SubtitleProofReport`` (runtime report, NOT an authoritative artifact)
   with SEPARATE verdict fields (PRD §7.4 — never one blended verdict).
5. FAIL-CLOSED — missing/corrupt sample or missing ASR input exits non-zero
   with a typed error naming the input and writes NO report.

Pure build steps live in ``_v44_subtitle_build``; T5's JP metrics bind in
``_v44_jp_metrics``; models + QC policy live in ``_v44_subtitle_report``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.analyze.asr_models import AsrError, AsrRequest, PinnedTool, TranscriptArtifact
from services.analyze.asr_whisper_cpp import DEFAULT_LOCK_PATH, transcribe
from services.cli._v44_jp_metrics import JpMetricsError, load_transcript_sample
from services.cli._v44_subtitle_build import (
    SubtitleProofError,
    build_plan,
    evidence_block,
    forward_segments,
    merged_proper_nouns,
    subtitle_side,
)
from services.cli._v44_subtitle_report import (
    AsrBlock,
    SampleBlock,
    SubtitleProofReport,
)
from services.creative_plan.subtitle_capability import SubtitlePlanError
from services.creative_plan.subtitle_text import ProperNounDictionaryError
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

REPORT_NAME = "subtitle-proof.json"
EXIT_TYPED_ERROR = 2


@dataclass(frozen=True, slots=True)
class ProofArgs:
    audio: Path | None
    asr_artifact: Path | None
    sample: Path
    out_dir: Path
    proper_nouns: Path | None
    episode_id: str
    lock_path: Path


def _live_request(audio: Path, lock_path: Path) -> AsrRequest:
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise SubtitleProofError("lock-wrong-type", f"{lock_path} is not phase-1-technical")
    section = lock.whisper_ja
    return AsrRequest(
        input_media_path=str(audio.resolve()),
        input_media_sha256=sha256_file(audio),
        model=PinnedTool(path=section.model.path, sha256=section.model.sha256),
        cli=PinnedTool(path=section.whisper_cli.path, sha256=section.whisper_cli.sha256),
        ffmpeg=PinnedTool(path=lock.ffmpeg.ffmpeg.path, sha256=lock.ffmpeg.ffmpeg.sha256),
    )


def _load_transcript(args: ProofArgs) -> tuple[TranscriptArtifact, AsrBlock]:
    if args.asr_artifact is not None and args.audio is not None:
        raise SubtitleProofError(
            "asr-input-ambiguous", "pass either --audio or --asr-artifact, not both"
        )
    if args.asr_artifact is not None:
        path = args.asr_artifact
        if not path.is_file():
            raise SubtitleProofError(
                "asr-artifact-missing", f"recorded artifact not found: {path}"
            )
        try:
            artifact = TranscriptArtifact.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise SubtitleProofError(
                "asr-artifact-parse-error", f"recorded artifact invalid at {path}: {error}"
            ) from error
        return artifact, AsrBlock(
            mode="replay",
            artifact_path=str(path),
            artifact_sha256=sha256_file(path),
            segment_count=len(artifact.segments),
            skipped_empty_segments=0,
        )
    if args.audio is None:
        raise SubtitleProofError(
            "asr-input-missing", "no ASR input: pass --audio (live) or --asr-artifact (replay)"
        )
    if not args.audio.is_file():
        raise SubtitleProofError("asr-input-missing", f"input audio not found: {args.audio}")
    result = transcribe(
        _live_request(args.audio, args.lock_path),
        work_dir=args.out_dir / "asr-work",
        cache_dir=args.out_dir / "asr-cache",
        lock_path=args.lock_path,
    )
    return result.artifact, AsrBlock(
        mode="live",
        artifact_path=result.artifact_path,
        artifact_sha256=sha256_file(Path(result.artifact_path)),
        segment_count=len(result.artifact.segments),
        skipped_empty_segments=0,
        cache_status=result.cache_status,
        input_media_path=str(args.audio),
        input_media_sha256=sha256_file(args.audio),
    )


def run_proof(args: ProofArgs) -> SubtitleProofReport:
    """Execute the full proof; writes the snippet + report only on success."""
    sample, sample_sha = load_transcript_sample(args.sample)
    dictionary = merged_proper_nouns(args.proper_nouns)
    artifact, asr_block = _load_transcript(args)
    segments, skipped = forward_segments(artifact)
    asr_block = asr_block.model_copy(update={"skipped_empty_segments": skipped})
    plan = build_plan(segments, episode_id=args.episode_id, dictionary=dictionary)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    subtitle = subtitle_side(plan, args.out_dir, (asr_block.artifact_sha256, sample_sha))
    evidence = evidence_block(segments, sample, dictionary)
    notes = [
        (
            f"capability: matrix subtitle-capability={subtitle.matrix_status} "
            f"({subtitle.capability_path} first); snippet rendered via the external "
            "ASS/SRT rung (mcp-fit probe-level caveat)"
        ),
    ]
    if asr_block.mode == "replay":
        notes.append("asr: replay of recorded artifact bytes (hash in asr block); no whisper run")
    else:
        notes.append(f"asr: live pinned whisper.cpp run (cache {asr_block.cache_status})")
    notes.append(f"evidence metrics source: {evidence.metrics_source}")

    report = SubtitleProofReport(
        schema_version="v44-subtitle-proof-v1",
        episode_id=args.episode_id,
        evidence_verdict="measured",
        subtitle_verdict="clean" if subtitle.qc_issue_count == 0 else "issues-found",
        asr=asr_block,
        evidence=evidence,
        subtitle=subtitle,
        sample=SampleBlock(
            path=str(args.sample),
            sha256=sample_sha,
            entries=len(sample.segments),
            proper_nouns=len(sample.proper_nouns),
        ),
        notes=tuple(notes),
    )
    atomic_write(args.out_dir / REPORT_NAME, canonical_model_bytes(report))
    return report


def _error_line(error: Exception) -> str:
    label = getattr(error, "label", None)
    if isinstance(label, str):
        return f"{label}: {getattr(error, 'detail', '')}"
    if isinstance(error, SubtitlePlanError):
        return f"subtitle-plan-{error.code}: {error.detail}"
    return f"{type(error).__name__}: {error}"


def _parse_args(argv: list[str] | None) -> ProofArgs:
    parser = argparse.ArgumentParser(
        prog="services.cli.v44_subtitle_proof",
        description="Japanese subtitle live-proof harness (evidence side measured separately)",
    )
    parser.add_argument(
        "--sample", required=True, type=Path, help="corrected transcript sample JSON"
    )
    parser.add_argument("--out-dir", required=True, type=Path, help="report output directory")
    parser.add_argument("--audio", type=Path, default=None, help="live ASR input media")
    parser.add_argument(
        "--asr-artifact", type=Path, default=None, help="replay a recorded transcript artifact JSON"
    )
    parser.add_argument(
        "--proper-nouns", type=Path, default=None, help="episode-local proper-noun additions JSON"
    )
    parser.add_argument("--episode-id", default="v44-real-01")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    parsed = parser.parse_args(argv)
    return ProofArgs(
        audio=parsed.audio,
        asr_artifact=parsed.asr_artifact,
        sample=parsed.sample,
        out_dir=parsed.out_dir,
        proper_nouns=parsed.proper_nouns,
        episode_id=parsed.episode_id,
        lock_path=parsed.lock,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = run_proof(args)
    except (
        SubtitleProofError,
        JpMetricsError,
        AsrError,
        ProperNounDictionaryError,
        SubtitlePlanError,
    ) as error:
        print(_error_line(error), file=sys.stderr)
        return EXIT_TYPED_ERROR
    print(f"subtitle proof: {args.out_dir / REPORT_NAME}")
    print(
        f"  evidence: cer={report.evidence.transcript_cer:.4f} "
        f"recall={report.evidence.proper_noun_recall:.2f} "
        f"p95_ms={report.evidence.timestamp_error_p95_ms} "
        f"omitted={report.evidence.omitted_utterances} "
        f"duplicated={report.evidence.duplicated_utterances}"
    )
    print(
        f"  subtitle: cues={report.subtitle.cue_count} "
        f"qc_issues={report.subtitle.qc_issue_count} "
        f"rule_ids={list(report.subtitle.qc_rule_ids)}"
    )
    print(f"  verdicts: evidence={report.evidence_verdict} subtitle={report.subtitle_verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
