"""REAL analyzer orchestration for the real-episode chain (Todo 46).

The Todo-38 orchestrator runs the REAL pinned analyzers in two sequential
passes (world-hash cache keys, immutable publication, index rebuild):
whisper.cpp Japanese ASR first, then the Todo-34 dialogue stack (over a
pinned-ffmpeg 48 kHz mono WAV) plus the Todo-35 visual stack in parallel.
Published refs from both passes become the evidence index.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.analysis_models import StreamFacts
from services.analyze.asr_models import AsrRequest, PinnedTool, TranscriptArtifact
from services.analyze.asr_whisper_cpp import transcribe
from services.analyze.audio_analysis import AudioAnalysisRequest, analyze_dialogue
from services.analyze.candidate_models import AnalysisArtifact
from services.analyze.orchestrator import AnalyzeOrchestrator
from services.analyze.orchestrator_models import (
    AnalyzerArtifact,
    AnalyzerSpec,
    EditSourceFile,
    EditSourceRef,
    OrchestrationRecord,
    spec_parameter_hash,
)
from services.analyze.visual_analysis import VisualAnalysisRequest, analyze_visual
from services.analyze.visual_models import VisualAnalysisArtifact
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli.media import AUDIO_ROLE, VIDEO_ROLE
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.contracts.primitives import ArtifactRef
    from services.toolchain.models import Phase1TechnicalToolchainLock

TRANSCRIPT_NAME: Final = "asr-whisper-cpp"
ANALYSIS_NAME: Final = "analyze-dialogue"
VISUAL_NAME: Final = "analyze-visual"
WAV_TIMEOUT_SECONDS: Final = 300


class RealAnalyzerError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RealAnalysis:
    record: OrchestrationRecord
    transcript: TranscriptArtifact
    dialogue: AnalysisArtifact
    visual: VisualAnalysisArtifact
    evidence: tuple[ArtifactRef, ...]


def edit_source_for(mezzanine: Path, wav: Path, source_id: str) -> EditSourceRef:
    """Video role binds the mezzanine; audio role binds the mono wav analyzer input."""

    return EditSourceRef(
        source_id=source_id,
        files=(
            EditSourceFile(
                role=VIDEO_ROLE, path=str(mezzanine.resolve()), sha256=sha256_file(mezzanine)
            ),
            EditSourceFile(
                role=AUDIO_ROLE, path=str(wav.resolve()), sha256=sha256_file(wav)
            ),
        ),
    )


def _mono_wav(ffmpeg: Path, mezzanine: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = (
        str(ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(mezzanine),
        "-vn",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(out),
    )
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=WAV_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise RealAnalyzerError(
            "wav_extract_timeout", f"mono wav extraction exceeded {WAV_TIMEOUT_SECONDS}s"
        ) from error
    if result.returncode != 0 or not out.is_file():
        raise RealAnalyzerError(
            "wav_extract_failed", result.stderr.strip()[-800:] or "no wav produced"
        )
    return out


def _spec(name: str, version: str, episode_id: str, profile: str) -> AnalyzerSpec:
    return AnalyzerSpec(
        analyzer_name=name,
        analyzer_version=version,
        parameter_hash=spec_parameter_hash({"episode": episode_id, "profile": profile}),
    )


def _published(record: OrchestrationRecord) -> tuple[ArtifactRef, ...]:
    return tuple(item.artifact for item in record.results if item.artifact is not None)


def _check_failures(record: OrchestrationRecord) -> None:
    if record.failures:
        first = record.failures[0]
        raise RealAnalyzerError("analyzer_failed", f"{first.error_label}: {first.detail}")


def _transcript_pass(  # noqa: PLR0913, PLR0917 (pass wiring: orchestrator + media + dirs)
    orchestrator: AnalyzeOrchestrator,
    edit_source: EditSourceRef,
    lock: Phase1TechnicalToolchainLock,
    mezzanine: Path,
    episode_id: str,
    out_dir: Path,
) -> tuple[TranscriptArtifact, OrchestrationRecord]:
    section = lock.whisper_ja
    outcome = transcribe(
        AsrRequest(
            input_media_path=str(mezzanine.resolve()),
            input_media_sha256=sha256_file(mezzanine),
            model=PinnedTool(path=section.model.path, sha256=section.model.sha256),
            cli=PinnedTool(path=section.whisper_cli.path, sha256=section.whisper_cli.sha256),
            ffmpeg=PinnedTool(
                path=lock.ffmpeg.ffmpeg.path, sha256=lock.ffmpeg.ffmpeg.sha256
            ),
        ),
        work_dir=out_dir / "episode" / "asr-work", cache_dir=out_dir / "asr-cache",
    )
    spec = _spec(TRANSCRIPT_NAME, "todo33-v1", episode_id, "whisper-ja-v1")
    record = orchestrator.run_analyzers(
        edit_source, (spec,), lambda _s, _src, _w: outcome.artifact
    )
    _check_failures(record)
    return outcome.artifact, record


def _dependent_pass(  # noqa: PLR0913, PLR0917 (one runner wiring both dependent analyzers)
    orchestrator: AnalyzeOrchestrator,
    edit_source: EditSourceRef,
    mezzanine: Path,
    wav: Path,
    transcript: TranscriptArtifact,
    episode_id: str,
    out_dir: Path,
) -> tuple[OrchestrationRecord, AnalysisArtifact, VisualAnalysisArtifact]:
    wav_sha = sha256_file(wav)
    media_sha = sha256_file(mezzanine)
    resolved = str(mezzanine.resolve())
    artifacts: dict[str, AnalyzerArtifact] = {}

    def runner(spec: AnalyzerSpec, _source: EditSourceRef, work_dir: Path) -> AnalyzerArtifact:
        if spec.analyzer_name == ANALYSIS_NAME:
            result = analyze_dialogue(
                AudioAnalysisRequest(
                    wav_path=str(wav),
                    wav_sha256=wav_sha,
                    declared=StreamFacts(sample_rate=48000),
                    transcript=transcript,
                    fixture_only=False,
                ),
                work_dir=work_dir, cache_dir=out_dir / "analysis-cache",
            )
            artifacts[ANALYSIS_NAME] = result.artifact
            return result.artifact
        if spec.analyzer_name == VISUAL_NAME:
            result = analyze_visual(
                VisualAnalysisRequest(
                    media_path=resolved, media_sha256=media_sha, fixture_only=False
                ),
                work_dir=work_dir,
            )
            artifacts[VISUAL_NAME] = result.artifact
            return result.artifact
        raise RealAnalyzerError("spec_unknown", f"no real analyzer for {spec.analyzer_name}")

    specs = (
        _spec(ANALYSIS_NAME, "todo34-v1", episode_id, "dialogue-v1"),
        _spec(VISUAL_NAME, "todo35-v1", episode_id, "visual-v1"),
    )
    record = orchestrator.run_analyzers(edit_source, specs, runner)
    _check_failures(record)
    dialogue = artifacts[ANALYSIS_NAME]
    visual = artifacts[VISUAL_NAME]
    if not isinstance(dialogue, AnalysisArtifact) or not isinstance(
        visual, VisualAnalysisArtifact
    ):
        raise RealAnalyzerError("artifact_type_wrong", "dependent pass returned wrong types")
    return record, dialogue, visual


def run_real_analyzers(
    lock: Phase1TechnicalToolchainLock,
    mezzanine: Path,
    source_id: str,
    episode_id: str,
    out_dir: Path,
) -> RealAnalysis:
    """Run the three REAL analyzers (ASR first, then dialogue + visual)."""

    orchestrator = AnalyzeOrchestrator(
        store=ArtifactStore(out_dir / "artifacts"),
        registry=ArtifactRegistry(out_dir / "registry"),
        episode_dir=out_dir / "episode",
        state_root=out_dir / "episode" / "analyze-state",
        work_root=out_dir / "episode" / "analyze-work",
    )
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    wav = _mono_wav(ffmpeg, mezzanine, out_dir / "episode" / "edit-source-mono.wav")
    edit_source = edit_source_for(mezzanine, wav, source_id)
    transcript, transcript_record = _transcript_pass(
        orchestrator, edit_source, lock, mezzanine, episode_id, out_dir
    )
    record, dialogue, visual = _dependent_pass(
        orchestrator, edit_source, mezzanine, wav, transcript, episode_id, out_dir
    )
    return RealAnalysis(
        record=record,
        transcript=transcript,
        dialogue=dialogue,
        visual=visual,
        evidence=(*_published(transcript_record), *_published(record)),
    )


__all__ = ["RealAnalysis", "RealAnalyzerError", "edit_source_for", "run_real_analyzers"]
