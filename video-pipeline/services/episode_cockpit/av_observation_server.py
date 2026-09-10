# allow: SIZE_OK — the chunked AV flow (extract + observe + GLM/ASR backfill
# + per-chunk provenance) is one orchestrated flow; splitting it would scatter
# the single flow across modules with no second use site.
"""windows=None server picking via chunked Gemini AV observation.

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
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.episode_cockpit.consultation_store import latest_adopted_policy
from services.foundation_io import atomic_write, sha256_file
from services.media_intelligence.gemini_av_models import AvChunk, extend_core
from services.media_intelligence.gemini_av_wire import (
    AvChunkOutcome,
    cached_chunk_outcome,
    observe_chunk_av,
)
from services.media_intelligence.observation_route import read_av_consent
from services.media_intelligence.sample_observation import (
    SampleCandidate,
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    observation_record,
    partition_chunks,
    pick_candidate_anchors,
    resolve_observed_windows,
    slice_transcript,
)

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C
    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.gemini_av_wire import GeminiAvHttp

_CONFIG_ROOT = Path(__file__).resolve().parents[2]
_GEMINI_AV_PIN_RELATIVE = (
    Path("config") / "toolchains" / "pins" / "moment-review-gemini-av.json"
)
_MEZZANINE_RELATIVE = ("run", "media", "edit-source.mov")
_AV_PROXY_DIR_NAME = "av-proxy"
_AV_PROXY_NAME = "proxy-480p-audio.mp4"
_AV_PROXY_SOURCE_SIDECAR = "proxy-480p-audio.source.json"
#: Whole-proxy encode budget (the 282s A/B proxy builds in well under this).
_AV_PROXY_TIMEOUT_S = 600
_AV_CHUNK_DIR_NAME = "av-chunks"
_AV_CHUNK_TIMEOUT_S = 300


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


def ensure_av_proxy(*, ffmpeg: Path, mezzanine: Path, workspace_dir: Path) -> Path:
    """Build the low-res A/V proxy once per Edit Source.

    ``ffmpeg -vf scale=480:-2 -c:v h264_videotoolbox -b:v 250k -maxrate 350k
    -bufsize 700k -c:a aac -b:a 96k -movflags +faststart`` — sized to the
    A/B-measured proxy (282s→7.7MB, 25,662 VIDEO tokens ≈ $0.008/call at LOW
    resolution). The pinned toolchain ffmpeg is a VideoToolbox-only build
    (no libx264), so crf is not an option there. Rebuilds only when the
    Edit Source size/mtime moved; the proxy is rebuildable runtime state
    under the episode dir.
    """

    from services.analyze.audio_probe import run_bounded  # noqa: PLC0415 (bounded run)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    target = workspace_dir / _AV_PROXY_NAME
    sidecar = workspace_dir / _AV_PROXY_SOURCE_SIDECAR
    try:
        stat = mezzanine.stat()
        fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"cannot stat the edit source media: {type(error).__name__}",
        ) from error
    if target.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")) == fingerprint:
                return target
        except (OSError, ValueError):
            pass
    argv = (
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(mezzanine),
        "-vf",
        "scale=480:-2",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        "250k",
        "-maxrate",
        "350k",
        "-bufsize",
        "700k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(target),
    )
    try:
        result = run_bounded(argv, _AV_PROXY_TIMEOUT_S, "AV proxy encode")
    except Exception as error:
        raise SampleObservationError(
            "sample-av-proxy-failed",
            f"the AV proxy encode failed ({type(error).__name__}); no detail echoed",
        ) from error
    if result.returncode != 0 or not target.is_file():
        raise SampleObservationError(
            "sample-av-proxy-failed",
            "the AV proxy encode failed; ffmpeg stderr is suppressed "
            "(paths never enter typed errors)",
        )
    atomic_write(sidecar, (json.dumps(fingerprint, sort_keys=True) + "\n").encode())
    return target


def _extract_av_chunk(
    *,
    ffmpeg: Path,
    proxy: Path,
    chunk: AvChunk,
    workspace_dir: Path,
) -> Path:
    """Slice one context-extended chunk clip from the whole proxy.

    Same VideoToolbox recipe as the proxy (the pinned toolchain build has
    no libx264). Skips re-encode when the proxy fingerprint + clip bounds
    match the sidecar; the clip is rebuildable runtime state.
    """

    from services.analyze.audio_probe import run_bounded  # noqa: PLC0415 (bounded run)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    target = workspace_dir / f"chunk-{chunk.index:03d}.mp4"
    sidecar = workspace_dir / f"chunk-{chunk.index:03d}.source.json"
    try:
        stat = proxy.stat()
        fingerprint = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "clip_start": chunk.clip_start,
            "clip_end": chunk.clip_end,
        }
    except OSError as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"cannot stat the AV proxy: {type(error).__name__}",
        ) from error
    if target.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")) == fingerprint:
                return target
        except (OSError, ValueError):
            pass
    duration = chunk.clip_end - chunk.clip_start
    argv = (
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{chunk.clip_start:.3f}",
        "-i",
        str(proxy),
        "-t",
        f"{duration:.3f}",
        "-vf",
        "scale=480:-2",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        "250k",
        "-maxrate",
        "350k",
        "-bufsize",
        "700k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(target),
    )
    try:
        result = run_bounded(argv, _AV_CHUNK_TIMEOUT_S, "AV chunk encode")
    except Exception as error:
        raise SampleObservationError(
            "sample-av-proxy-failed",
            f"the AV chunk encode failed ({type(error).__name__}); no detail echoed",
        ) from error
    if result.returncode != 0 or not target.is_file():
        raise SampleObservationError(
            "sample-av-proxy-failed",
            "the AV chunk encode failed; ffmpeg stderr is suppressed "
            "(paths never enter typed errors)",
        )
    atomic_write(sidecar, (json.dumps(fingerprint, sort_keys=True) + "\n").encode())
    return target


def _insufficient(core: SampleChunk, note: str) -> SampleChunkObservation:
    return SampleChunkObservation(
        chunk_index=core.index,
        chunk_start_seconds=core.start_seconds,
        chunk_end_seconds=core.end_seconds,
        findings=("chunk yielded no usable observation",),
        quality_insufficient=True,
        insufficiency_note=note,
    )


def _chunk_observation_from_outcome(
    outcome: AvChunkOutcome, core: SampleChunk
) -> SampleChunkObservation:
    """Adopted GLOBAL events as ONE core-bounded chunk observation.

    Candidates are the adopted events carrying a reason, clamped into the
    core (an adopted start is always in-core; only the end can poke into
    the next core's context). The shared pick/widen machinery runs
    unchanged downstream.
    """

    findings = tuple(
        event.visual.strip() or "(no visual description)" for event in outcome.events
    ) or ("(no adopted core events)",)
    candidates: list[SampleCandidate] = []
    for event in outcome.events:
        reason = event.sample_candidate_reason.strip()
        if not reason:
            continue
        start = max(event.start_seconds, core.start_seconds)
        end = min(event.end_seconds, core.end_seconds)
        if end < start:
            continue
        candidates.append(
            SampleCandidate(start_second=start, end_second=end, reason=reason)
        )
    return SampleChunkObservation(
        chunk_index=core.index,
        chunk_start_seconds=core.start_seconds,
        chunk_end_seconds=core.end_seconds,
        findings=findings,
        candidates=tuple(candidates),
        uncertainty=outcome.uncertain_flags or None,
        quality_insufficient=outcome.chunk_insufficient,
        insufficiency_note=None,
    )


@dataclass
class _AvChunkRunner:
    """One Gemini attempt per core, accumulating observations + provenance.

    A failed/insufficient chunk is backfilled by the GLM silent path plus
    the ASR slice (never a Gemini retry); the journal then records which
    cores are gemini-ok, glm-backfilled, or still insufficient.
    """

    ffmpeg: Path
    proxy: Path
    chunk_workspace: Path
    transport: GeminiAvHttp
    pin: EditorialPinV2
    env: dict[str, str]
    episode_dir: Path
    episode_id: str
    proxy_sha: str
    backfill: _ChunkBackfill
    observations: list[SampleChunkObservation] = field(default_factory=list)
    provenance: list[dict[str, object]] = field(default_factory=list)
    meters: list[dict[str, object]] = field(default_factory=list)
    drifts: list[dict[str, object]] = field(default_factory=list)

    def run(self, core: SampleChunk, chunk: AvChunk) -> None:
        try:
            outcome = cached_chunk_outcome(
                episode_dir=self.episode_dir,
                proxy_sha256=self.proxy_sha,
                pin=self.pin,
                chunk=chunk,
            )
            if outcome is None:
                clip = _extract_av_chunk(
                    ffmpeg=self.ffmpeg,
                    proxy=self.proxy,
                    chunk=chunk,
                    workspace_dir=self.chunk_workspace,
                )
                outcome = observe_chunk_av(
                    self.transport,
                    pin=self.pin,
                    env=self.env,
                    episode_dir=self.episode_dir,
                    episode_id=self.episode_id,
                    proxy_sha256=self.proxy_sha,
                    chunk=chunk,
                    clip_path=clip,
                )
        except SampleObservationError as error:
            self.observations.append(
                _backfilled(core, self.backfill, error.code, self.provenance)
            )
            return
        self.meters.append(_meter_entry(chunk.index, outcome))
        self.drifts.extend(
            {
                "chunk_index": core.index,
                "index": flag.index,
                "raw_start": flag.raw_start,
                "raw_end": flag.raw_end,
            }
            for flag in outcome.drift_flags
        )
        if outcome.chunk_insufficient or not outcome.events:
            note = (
                f"{len(outcome.drift_flags)} clip-local event(s) out of range "
                "(no clamp, no rescue)"
                if outcome.chunk_insufficient
                else "no adopted core events"
            )
            self.observations.append(
                _backfilled(core, self.backfill, note, self.provenance)
            )
            return
        self.observations.append(_chunk_observation_from_outcome(outcome, core))
        self.provenance.append(
            {
                "index": core.index,
                "core_start_seconds": core.start_seconds,
                "core_end_seconds": core.end_seconds,
                "status": "gemini-ok",
                "detail": (
                    f"adopted {len(outcome.events)} core event(s); "
                    f"reused={outcome.reused}"
                ),
            }
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


def _av_journal_record(
    *,
    cores: tuple[SampleChunk, ...],
    runner: _AvChunkRunner,
    policy_text: str,
) -> dict[str, object]:
    """Shared journal plus additive per-chunk AV provenance (no new artifact).

    ``av_chunk_provenance`` says per core whether Gemini, the GLM/ASR
    backfill, or neither produced the observation; each chunk entry
    carries ``av_status`` / ``av_speech_source`` / ``av_audio_unconfirmed``
    (ambient/music/audiovisual_relation stay UNCONFIRMED off the Gemini
    path). ``meter`` sums the metered Gemini chunk calls.
    """

    observations, provenance, meters, drifts = (
        runner.observations,
        runner.provenance,
        runner.meters,
        runner.drifts,
    )
    anchors = pick_candidate_anchors(observations)
    record = observation_record(cores, observations, anchors)
    record["route"] = "audiovisual"
    record["timestamp_drift"] = drifts
    status_by_index = {entry["index"]: entry["status"] for entry in provenance}
    raw_chunks = record.get("chunks")
    journal_entries = raw_chunks if isinstance(raw_chunks, list) else []
    for entry in journal_entries:
        if not isinstance(entry, dict):
            continue
        status = status_by_index.get(entry["index"], "gemini-insufficient")
        entry["av_status"] = status
        if status == "gemini-ok":
            entry["av_speech_source"] = "gemini"
        elif status == "glm-backfilled":
            entry["av_speech_source"] = "asr-slice"
        else:
            entry["av_speech_source"] = "none"
        entry["av_audio_unconfirmed"] = status != "gemini-ok"
    ok = [entry["index"] for entry in provenance if entry["status"] == "gemini-ok"]
    backfilled = [
        entry["index"] for entry in provenance if entry["status"] == "glm-backfilled"
    ]
    record["audio_integration"] = "confirmed" if len(ok) == len(cores) else "partial"
    record["av_chunk_provenance"] = provenance
    record["av_chunks_gemini_ok"] = ok
    record["av_chunks_glm_backfilled"] = backfilled
    record["av_chunks_insufficient"] = [
        entry["index"] for entry in provenance if entry["status"] == "gemini-insufficient"
    ]
    meter: dict[str, object] = {
        "chunks": meters,
        "total_usd": round(sum(_number(entry.get("total_usd")) for entry in meters), 6),
        "cost_method": "sum of per-chunk Gemini calls (GLM backfill is not metered here)",
        "result_reused": all(entry["reused"] for entry in meters)
        and len(meters) == len(cores),
    }
    record["meter"] = meter
    record["result_reused"] = meter["result_reused"]
    record["policy_text"] = policy_text  # comparison CONDITIONS only, never commands
    return record


def _adopted_policy_text(episode_dir: Path) -> str:
    adopted = latest_adopted_policy(episode_dir)
    return adopted.model_dump_json() if adopted is not None else ""


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _meter_entry(index: int, outcome: AvChunkOutcome) -> dict[str, object]:
    meter = outcome.meter
    cost = meter.get("cost_usd")
    cost_map = cost if isinstance(cost, dict) else {}
    total = cost_map.get("total_usd")
    if not isinstance(total, (int, float)):
        total = float(cost_map.get("input_usd_upper", cost_map.get("input_usd", 0.0))) + float(
            cost_map.get("output_usd", 0.0)
        )
    tokens = meter.get("tokens")
    return {
        "index": index,
        "model": meter.get("model"),
        "latency_ms": meter.get("latency_ms"),
        "tokens": tokens if isinstance(tokens, dict) else {},
        "cost_usd": cost_map,
        "total_usd": round(float(total), 6),
        "reused": outcome.reused,
    }


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


__all__ = ["ensure_av_proxy", "resolve_av_sample_windows"]
