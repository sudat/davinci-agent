"""windows=None server picking via GLM full-source chunked observation.

Assembles mezzanine + transcript + GLM provider around the two
media_intelligence halves. Every failure is SampleObservationError, which
the route maps to a typed 422 — position sampling is never a fallback, and
Gemini is never called. Module-level seam so tests inject fixtures.
"""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.episode_cockpit.consultation_store import latest_adopted_policy
from services.media_intelligence.sample_observation import (
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    chunk_frame_range,
    observation_record,
    partition_chunks,
    pick_candidate_anchors,
    resolve_observed_windows,
    slice_transcript,
)

if TYPE_CHECKING:
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C
    from services.media_intelligence.video_review_providers import (
        GlmVisualSpecialistProvider,
    )

_CONFIG_ROOT = Path(__file__).resolve().parents[2]
_GLM_PIN_RELATIVE = (
    Path("config") / "toolchains" / "pins" / "moment-review-glm-visual.json"
)
_MEZZANINE_RELATIVE = ("run", "media", "edit-source.mov")
_INDEX_NAME = "media-intelligence.duckdb"
_WORKSPACE_DIR_NAME = "sample-observation-clips"


def _insufficient(chunk: SampleChunk, note: str) -> SampleChunkObservation:
    return SampleChunkObservation(
        chunk_index=chunk.index,
        chunk_start_seconds=chunk.start_seconds,
        chunk_end_seconds=chunk.end_seconds,
        findings=("chunk yielded no usable observation",),
        quality_insufficient=True,
        insufficiency_note=note,
    )


def _transcript_segments(
    episode_dir: Path, total_frames: int, rate: Fraction
) -> list[tuple[float, float, str]]:
    from services.media_query import v2_models as vm  # noqa: PLC0415 (duckdb off the fast path)
    from services.media_query.index_v2 import open_read_only  # noqa: PLC0415
    from services.media_query.query_v2 import MediaQueryApiV2  # noqa: PLC0415

    index_path = episode_dir / _INDEX_NAME
    if not index_path.is_file():
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"no media-intelligence index at {index_path}; cannot slice speech",
        )
    try:
        connection = open_read_only(index_path)
        try:
            row = connection.execute(
                "SELECT source_id FROM mi_sources ORDER BY source_id LIMIT 1"
            ).fetchone()
            if row is None:
                raise SampleObservationError(
                    "sample-observation-unavailable",
                    f"index {index_path} carries no source",
                )
            api = MediaQueryApiV2(connection)
            segments: list[tuple[float, float, str]] = []
            offset = 0
            while True:
                page = api.transcript_range(
                    vm.TranscriptRangeRequest(
                        source_id=str(row[0]),
                        span=vm.FrameSpan(start_frame=0, end_frame=total_frames),
                        pagination=vm.V2Pagination(
                            limit=vm.V2_MAX_PAGE_SIZE, offset=offset
                        ),
                    )
                )
                fps = float(rate.numerator) / float(rate.denominator)
                segments.extend(
                    (
                        float(entry.span.start_frame) / fps,
                        float(entry.span.end_frame) / fps,
                        entry.text,
                    )
                    for entry in page.rows
                )
                offset += len(page.rows)
                if not page.rows or offset >= page.total:
                    return segments
        finally:
            connection.close()
    except SampleObservationError:
        raise
    except Exception as error:
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"cannot read transcript rows: {error}",
        ) from error


def _glm_provider(episode_id: str) -> GlmVisualSpecialistProvider:
    from services.cli._v44_arm_video_factory import (  # noqa: PLC0415 (CLI owns transport)
        make_video_http_post,
    )
    from services.cli.real_policy import (  # noqa: PLC0415 (CLI owns the data policy)
        video_understanding_policy,
    )
    from services.editorial_v2.editorial_pins import (  # noqa: PLC0415 (pin load stays off the fast path)
        EditorialRuntimeError,
        load_editorial_pin,
        require_pin_env,
    )
    from services.media_intelligence.video_review_providers import (  # noqa: PLC0415
        GlmVisualSpecialistProvider,
    )

    try:
        pin = load_editorial_pin(_CONFIG_ROOT / _GLM_PIN_RELATIVE)
        provider = GlmVisualSpecialistProvider(
            pin=pin,
            transport=make_video_http_post(),
            policy=video_understanding_policy(episode_id),
            env=dict(os.environ),
            episode_id=episode_id,
        )
        require_pin_env(pin, provider.env)
    except EditorialRuntimeError as error:
        raise SampleObservationError(
            "sample-observation-unavailable", f"GLM pin or credentials: {error}"
        ) from error
    return provider


def resolve_server_sample_windows(
    full_ir: TimelineIr0C, episode_dir: Path, episode_id: str
) -> tuple[list[RecordFrameSpan], str]:
    """Observe the whole edit source in 60s chunks and resolve widened
    windows plus the journal-ready observation record (JSON string)."""

    from services.analyze.audio_probe import (  # noqa: PLC0415 (pinned tools off the fast path)
        resolve_audio_tools,
    )
    from services.analyze.visual_decode import probe_video_facts  # noqa: PLC0415
    from services.media_intelligence.moment_review import (  # noqa: PLC0415 (review models off the fast path)
        ReviewWindow,
    )
    from services.media_intelligence.sample_observation_wire import (  # noqa: PLC0415
        observe_chunk,
        render_untrusted_data,
    )
    from services.media_intelligence.video_clip_evidence import VideoClipError  # noqa: PLC0415
    from services.media_intelligence.video_clip_extraction import ClipExtractor  # noqa: PLC0415
    from services.media_intelligence.video_review_wire import VideoProviderError  # noqa: PLC0415

    mezzanine = episode_dir.joinpath(*_MEZZANINE_RELATIVE)
    if not mezzanine.is_file():
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"edit source media is missing: {mezzanine}",
        )
    try:
        facts = probe_video_facts(resolve_audio_tools().ffprobe, mezzanine)
    except Exception as error:
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"cannot probe the edit source: {error}",
        ) from error
    rate = Fraction(facts.rate_num, facts.rate_den)
    chunks = partition_chunks(float(Fraction(facts.frame_count, 1) / rate))
    segments = _transcript_segments(episode_dir, facts.frame_count, rate)
    provider = _glm_provider(episode_id)
    policy = latest_adopted_policy(episode_dir)
    policy_text = policy.model_dump_json() if policy is not None else ""
    extractor = ClipExtractor(
        media_path=mezzanine,
        workspace_dir=episode_dir / "consultation" / _WORKSPACE_DIR_NAME,
    )
    observations: list[SampleChunkObservation] = []
    for chunk in chunks:
        start_frame, end_frame = chunk_frame_range(chunk, rate, facts.frame_count)
        try:
            pair = extractor.extract(
                ReviewWindow(start_frame=start_frame, end_frame=end_frame)
            )
        except VideoClipError as error:
            observations.append(_insufficient(chunk, f"clip extraction: {error.code}"))
            continue
        untrusted = render_untrusted_data(
            policy_text, slice_transcript(segments, chunk), chunk
        )
        try:
            observations.append(observe_chunk(provider, chunk, pair.glm, untrusted))
        except VideoProviderError as error:
            observations.append(_insufficient(chunk, error.code))
    anchors = pick_candidate_anchors(observations)
    record = observation_record(chunks, observations, anchors)
    windows = resolve_observed_windows(
        full_ir=full_ir, source_rate=rate, observations=observations
    )
    return list(windows), json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["resolve_server_sample_windows"]
