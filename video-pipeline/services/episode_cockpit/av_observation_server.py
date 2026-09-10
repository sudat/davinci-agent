"""windows=None server picking via chunked Gemini AV observation (route entry).

Assembles mezzanine + consent gate + A/V proxy + one pinned Gemini AV
call PER 60s core around the shared media_intelligence halves. Cores
partition the full duration exactly once; each call observes the core
extended by 3s context, reports clip-local seconds, and only core
starts are adopted (no boundary duplicates, no merge).

A failed/insufficient chunk is backfilled per chunk by the existing
GLM silent path (visual findings) plus the existing ASR slice (speech);
ambient/music/audiovisual_relation for those chunks stay UNCONFIRMED.
No auto-retry of any Gemini call. The consent gate fails typed without
consent — GLM is never silently substituted here; the ROUTER decides
fallback. Gemini speech is observation only (ASR stays the subtitle
authority); no model writes Job State, Selection Plan, or Edit Plan.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.episode_cockpit.av_chunk_media import (
    _AV_CHUNK_DIR_NAME,
    _AV_PROXY_DIR_NAME,
    _MEZZANINE_RELATIVE,
    ensure_av_proxy,
)
from services.episode_cockpit.av_observation_backfill import (
    _adopted_policy_text,
    _ChunkBackfill,
)
from services.episode_cockpit.av_observation_chunks import (
    _av_journal_record,
    _AvChunkRunner,
)
from services.foundation_io import sha256_file
from services.media_intelligence.gemini_av_models import extend_core
from services.media_intelligence.observation_route import read_av_consent
from services.media_intelligence.sample_observation import (
    SampleObservationError,
    partition_chunks,
    resolve_observed_windows,
)

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C
    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.gemini_av_files import GeminiAvHttp

_CONFIG_ROOT = Path(__file__).resolve().parents[2]
_GEMINI_AV_PIN_RELATIVE = (
    Path("config") / "toolchains" / "pins" / "moment-review-gemini-av.json"
)


def _require_consent(episode_dir: Path, episode_id: str) -> None:
    if not read_av_consent(episode_dir, episode_id):
        raise SampleObservationError(
            "sample-av-consent-missing",
            "the audiovisual observation needs the episode's explicit "
            "original_video+original_audio cloud-send consent "
            "(av-analysis-consent.json with cloud_media_av_analysis: true); "
            "refusing rather than substituting a visual-only observation",
        )


def _gemini_av_provider(
    episode_id: str,
) -> tuple[EditorialPinV2, GeminiAvHttp, dict[str, str], ResolvedConfig]:
    """Pin + injected transport + env for the AV calls (CLI owns sockets)."""

    from services.cli._v44_arm_video_factory import (  # noqa: PLC0415 (CLI owns transport)
        make_gemini_av_http,
    )
    from services.cli.real_policy import (  # noqa: PLC0415 (CLI owns the data policy)
        video_understanding_policy,
    )
    from services.editorial_v2.editorial_pins import (  # noqa: PLC0415 (pin load stays off the fast path)
        EditorialRuntimeError,
        load_editorial_pin,
    )

    try:
        pin = load_editorial_pin(_CONFIG_ROOT / _GEMINI_AV_PIN_RELATIVE)
    except EditorialRuntimeError as error:
        raise SampleObservationError(
            "sample-av-unavailable", f"Gemini AV pin: {error.code}"
        ) from None
    import os  # noqa: PLC0415 (env snapshot at call time, like the GLM path)

    return pin, make_gemini_av_http(), dict(os.environ), video_understanding_policy(
        episode_id
    )


def resolve_av_sample_windows(
    full_ir: TimelineIr0C, episode_dir: Path, episode_id: str
) -> tuple[list[RecordFrameSpan], str]:
    """Observe the edit source in one Gemini AV call PER 60s core; resolve
    widened windows plus the journal-ready observation record (JSON string).

    Per-chunk provenance (additive ``av_*`` fields) records which cores
    came from Gemini, which were GLM-backfilled, and which stayed
    insufficient. Zero usable candidates anywhere is a typed whole-route
    failure (the router falls back on ``auto``), as before.
    """

    from services.analyze.audio_probe import (  # noqa: PLC0415 (pinned tools off the fast path)
        resolve_audio_tools,
    )
    from services.analyze.visual_decode import probe_video_facts  # noqa: PLC0415
    from services.policy.data_policy import authorize_cloud_transport  # noqa: PLC0415

    _require_consent(episode_dir, episode_id)
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
            f"cannot probe the edit source: {type(error).__name__}",
        ) from error
    rate = Fraction(facts.rate_num, facts.rate_den)
    duration_seconds = float(Fraction(facts.frame_count, 1) / rate)
    pin, transport, env, policy = _gemini_av_provider(episode_id)
    from services.cli.real_policy import MOMENT_REVIEW_STAGE  # noqa: PLC0415

    for data_class in ("original_video", "audio"):
        decision = authorize_cloud_transport(
            policy,
            data_class=data_class,  # type: ignore[arg-type] (DataClass literal pair)
            stage=MOMENT_REVIEW_STAGE,
            episode_id=episode_id,
        )
        if not decision.allowed:
            raise SampleObservationError("sample-av-policy-denied", decision.reason)
    try:
        ffmpeg = resolve_audio_tools().ffmpeg
    except Exception as error:
        raise SampleObservationError(
            "sample-observation-unavailable",
            f"cannot resolve the pinned ffmpeg: {type(error).__name__}",
        ) from error
    proxy = ensure_av_proxy(
        ffmpeg=ffmpeg,
        mezzanine=mezzanine,
        workspace_dir=episode_dir / "consultation" / _AV_PROXY_DIR_NAME,
    )
    chunk_workspace = episode_dir / "consultation" / _AV_CHUNK_DIR_NAME
    proxy_sha = sha256_file(proxy)
    cores = partition_chunks(duration_seconds)
    chunks = tuple(
        extend_core(core.index, core.start_seconds, core.end_seconds, duration_seconds)
        for core in cores
    )
    backfill = _ChunkBackfill(episode_dir, episode_id, rate, facts.frame_count)
    policy_text = _adopted_policy_text(episode_dir)
    runner = _AvChunkRunner(
        ffmpeg=ffmpeg,
        proxy=proxy,
        chunk_workspace=chunk_workspace,
        transport=transport,
        pin=pin,
        env=env,
        episode_dir=episode_dir,
        episode_id=episode_id,
        proxy_sha=proxy_sha,
        backfill=backfill,
    )
    for core, chunk in zip(cores, chunks, strict=True):
        runner.run(core, chunk)
    record = _av_journal_record(cores=cores, runner=runner, policy_text=policy_text)
    windows = resolve_observed_windows(
        full_ir=full_ir, source_rate=rate, observations=runner.observations
    )
    return list(windows), json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["resolve_av_sample_windows"]
