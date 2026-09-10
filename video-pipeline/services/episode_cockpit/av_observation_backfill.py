"""Lazy GLM + ASR-slice backfill for failed/insufficient Gemini AV chunks.

A failed/insufficient chunk is backfilled per chunk by the existing GLM
silent path (visual findings) plus the existing ASR slice (speech);
ambient/music/audiovisual_relation for those chunks stay UNCONFIRMED.
Creation is lazy so an all-Gemini-ok run never touches GLM pins,
indexes, or clips.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from services.episode_cockpit.av_chunk_media import _MEZZANINE_RELATIVE
from services.episode_cockpit.consultation_store import latest_adopted_policy
from services.media_intelligence.sample_observation import (
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    slice_transcript,
)


def _insufficient(core: SampleChunk, note: str) -> SampleChunkObservation:
    return SampleChunkObservation(
        chunk_index=core.index,
        chunk_start_seconds=core.start_seconds,
        chunk_end_seconds=core.end_seconds,
        findings=("chunk yielded no usable observation",),
        quality_insufficient=True,
        insufficiency_note=note,
    )


def _adopted_policy_text(episode_dir: Path) -> str:
    adopted = latest_adopted_policy(episode_dir)
    return adopted.model_dump_json() if adopted is not None else ""


class _ChunkBackfill:
    """Lazy GLM + ASR-slice backfill for failed Gemini chunks.

    The GLM visual observation runs over the core's own mezzanine clip
    (video-only evidence — audio media never rides GLM); the ASR speech
    slice rides as untrusted text inside that same call. Creation is lazy
    so an all-Gemini-ok run never touches GLM pins, indexes, or clips.
    """

    def __init__(
        self, episode_dir: Path, episode_id: str, rate: Fraction, frame_count: int
    ) -> None:
        self._episode_dir = episode_dir
        self._episode_id = episode_id
        self._rate = rate
        self._frame_count = frame_count
        self._started = False
        self._unavailable: str | None = None
        self._segments: list[tuple[float, float, str]] = []
        self._policy_text = ""
        self._provider: Any = None
        self._extractor: Any = None
        self.asr_speech = False

    def run(self, core: SampleChunk) -> SampleChunkObservation:
        from services.media_intelligence.moment_review import (  # noqa: PLC0415 (review models off the fast path)
            ReviewWindow,
        )
        from services.media_intelligence.sample_observation import (  # noqa: PLC0415 (shared chunk math)
            chunk_frame_range,
        )
        from services.media_intelligence.sample_observation_wire import (  # noqa: PLC0415
            observe_chunk,
            render_untrusted_data,
        )
        from services.media_intelligence.video_clip_evidence import VideoClipError  # noqa: PLC0415
        from services.media_intelligence.video_review_wire import (  # noqa: PLC0415 (error vocabulary only)
            VideoProviderError,
        )

        if not self._started:
            self._start()
        if self._unavailable is not None:
            return _insufficient(core, self._unavailable)
        provider, extractor = self._provider, self._extractor
        if provider is None or extractor is None:
            return _insufficient(core, "glm backfill unavailable: late init failed")
        try:
            start_frame, end_frame = chunk_frame_range(
                core, self._rate, self._frame_count
            )
            pair = extractor.extract(
                ReviewWindow(start_frame=start_frame, end_frame=end_frame)
            )
        except VideoClipError as error:
            return _insufficient(core, f"glm backfill clip extraction: {error.code}")
        untrusted = render_untrusted_data(
            self._policy_text, slice_transcript(self._segments, core), core
        )
        try:
            return observe_chunk(provider, core, pair.glm, untrusted)
        except VideoProviderError as error:
            return _insufficient(core, f"glm backfill: {error.code}")

    def _start(self) -> None:
        from services.episode_cockpit.sample_observation_server import (  # noqa: PLC0415 (GLM path owns these seams)
            _glm_provider,
            _transcript_segments,
        )
        from services.media_intelligence.video_clip_extraction import (  # noqa: PLC0415 (pinned tools off the fast path)
            ClipExtractor,
        )

        self._started = True
        try:
            provider = _glm_provider(self._episode_id)
        except SampleObservationError as error:
            self._unavailable = f"glm backfill unavailable: {error.code}"
            return
        try:
            segments = _transcript_segments(
                self._episode_dir, self._frame_count, self._rate
            )
        except SampleObservationError:
            segments = []
        self._segments = segments
        self.asr_speech = bool(segments)
        self._policy_text = _adopted_policy_text(self._episode_dir)
        self._provider = provider
        self._extractor = ClipExtractor(
            media_path=self._episode_dir.joinpath(*_MEZZANINE_RELATIVE),
            workspace_dir=self._episode_dir / "consultation" / "sample-observation-clips",
        )


def _backfilled(
    core: SampleChunk,
    backfill: _ChunkBackfill,
    note: str,
    provenance: list[dict[str, object]],
) -> SampleChunkObservation:
    observation = backfill.run(core)
    if observation.quality_insufficient:
        status = "gemini-insufficient"
        detail = f"gemini failed ({note}); glm backfill failed ({observation.insufficiency_note})"
    else:
        status = "glm-backfilled"
        detail = (
            f"gemini failed ({note}); backfilled by GLM visual findings + "
            f"{'ASR speech slice' if backfill.asr_speech else 'no transcript (visual only)'}"
        )
    provenance.append(
        {
            "index": core.index,
            "core_start_seconds": core.start_seconds,
            "core_end_seconds": core.end_seconds,
            "status": status,
            "detail": detail,
        }
    )
    return observation
