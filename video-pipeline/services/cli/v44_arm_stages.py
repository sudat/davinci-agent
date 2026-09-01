"""The REAL chain stage runner for one V44-0 arm workspace (T14 enabler).

Extracted from ``v44_arm_pipeline`` to respect the 250-LOC module ceiling:
ingest → normalize (frozen cfr30) → real analyzers → speech pool, over a
fresh arm workspace, reusing the established chain machinery verbatim
(``episode_runner_workspace`` manifest, ``real_episode`` ingest,
``real_chain._normalize``, ``run_real_analyzers``, ``real_pool``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.cli.episode_runner_workspace import principal_video, write_chain_manifest
from services.cli.real_analyze import run_real_analyzers
from services.cli.real_chain import PHASE1_LOCK, _normalize
from services.cli.real_episode import ingest_real_episode, load_real_episode
from services.cli.real_pool import SpeechSegment, pool_for
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

_WHISPER_PIN_CANDIDATES: Final = (
    Path("config/toolchains/pins/whisper-ja-v2.json"),
    Path("config/toolchains/pins/whisper-ja.json"),
)


class ArmPipelineError(Exception):
    """Typed arm-pipeline refusal (code/detail)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ArmPipelineData:
    """What the real chain stages produced (injectable in tests)."""

    episode_id: str
    source_id: str
    total_frames: int
    speech: tuple[SpeechSegment, ...]
    transcript_segments_ms: tuple[tuple[int, int, str], ...]
    mezzanine: Path | None
    mezzanine_sha256: str | None


def run_arm_stages(
    episode_root: Path, workspace: Path, episode_id: str
) -> ArmPipelineData:
    """ingest → normalize → analyze → pool over a fresh arm workspace."""

    video = principal_video(episode_root / "sources")
    workspace.mkdir(parents=True, exist_ok=True)
    write_chain_manifest(workspace, episode_id, video)
    manifest = load_real_episode(workspace)
    lock = load_lock(PHASE1_LOCK)
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise ArmPipelineError(
            "lock_wrong", f"{PHASE1_LOCK} is not the phase-1 toolchain lock"
        )
    ffmpeg = Path(lock.ffmpeg.ffmpeg.path)
    ffprobe = Path(lock.ffmpeg.ffprobe.path)
    _source, _eligibility, _video_sha = ingest_real_episode(
        manifest, workspace, ffprobe, workspace
    )
    normalize_record, mezzanine = _normalize(
        workspace / "source-manifest.json", ffmpeg, ffprobe, workspace
    )
    total_frames = normalize_record.drop_dup.expected.output_frames
    source_id = f"{episode_id}-edit-source"
    analysis = run_real_analyzers(lock, mezzanine, source_id, episode_id, workspace)
    pool, speech = pool_for(
        analysis.transcript,
        analysis.dialogue,
        analysis.evidence,
        source_id=source_id,
        edit_source_sha=analysis.record.edit_source_sha256,
        total_frames=total_frames,
    )
    del pool  # the v2 editorial path consumes the speech segments, not the v1 pool
    return ArmPipelineData(
        episode_id=episode_id,
        source_id=source_id,
        total_frames=total_frames,
        speech=speech,
        transcript_segments_ms=tuple(
            (int(segment.start_ms), int(segment.end_ms), segment.text)
            for segment in analysis.transcript.segments
        ),
        mezzanine=mezzanine,
        mezzanine_sha256=normalize_record.output.sha256,
    )


def whisper_provider_pin(video_pipeline_root: Path) -> str:
    """``whisper-cpp-cli:<sha12>`` label from the whisper-ja pin (T17 convention)."""

    import json  # noqa: PLC0415

    for candidate in _WHISPER_PIN_CANDIDATES:
        path = video_pipeline_root / candidate
        if path.is_file():
            try:
                payload: object = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ArmPipelineError(
                    "whisper-pin-unreadable", f"cannot read {path}: {error}"
                ) from error
            if isinstance(payload, dict):
                cli = payload.get("whisper_cli")
                if isinstance(cli, dict) and isinstance(cli.get("sha256"), str):
                    return f"whisper-cpp-cli:{cli['sha256'][:12]}"
            raise ArmPipelineError(
                "whisper-pin-malformed", f"{path} carries no whisper_cli.sha256"
            )
    raise ArmPipelineError(
        "whisper-pin-missing", f"no whisper pin under {video_pipeline_root}"
    )


__all__ = [
    "ArmPipelineData",
    "ArmPipelineError",
    "run_arm_stages",
    "whisper_provider_pin",
]
