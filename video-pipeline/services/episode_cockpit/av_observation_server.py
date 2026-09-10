"""windows=None server picking via Gemini whole-video AV observation.

Assembles mezzanine + consent gate + A/V proxy + the pinned Gemini AV
call around the shared media_intelligence halves. The consent gate
(``av-analysis-consent.json``) fails typed without consent — GLM is
never silently substituted here; the ROUTER decides fallback. Every
other failure is ``SampleObservationError``, which the route maps to a
typed 422. Gemini speech is observation only (ASR stays the subtitle
authority); no model writes Job State, Selection Plan, or Edit Plan.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.episode_cockpit.consultation_store import latest_adopted_policy
from services.foundation_io import atomic_write
from services.media_intelligence.observation_route import read_av_consent
from services.media_intelligence.sample_observation import (
    MAX_SAMPLE_CANDIDATES,
    SampleCandidate,
    SampleChunk,
    SampleChunkObservation,
    SampleObservationError,
    observation_record,
    pick_candidate_anchors,
    resolve_observed_windows,
)

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C
    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.gemini_av_observation import (
        AvObservation,
        GeminiAvHttp,
    )

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
_ANCHOR_SPREAD_MIN_S = 8.0


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
    """Pin + injected transport + env for the AV call (CLI owns sockets)."""

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


def _av_observation_to_chunk(
    observation: AvObservation, duration_seconds: float
) -> SampleChunkObservation:
    """Wrap whole-video AV events as ONE pseudo-chunk (shared machinery).

    The deterministic candidate pick, journal record, and widening then
    run unchanged: candidates are the events carrying a
    ``sample_candidate_reason``; an insufficient AV observation yields no
    candidates, so resolution refuses typed downstream.
    """

    chunk = SampleChunk(index=0, start_seconds=0.0, end_seconds=duration_seconds)
    findings = tuple(
        event.visual.strip() or "(no visual description)"
        for event in observation.events
    ) or ("(no AV events returned)",)
    candidates = tuple(
        SampleCandidate(
            start_second=event.start_seconds,
            end_second=event.end_seconds,
            reason=event.sample_candidate_reason.strip(),
        )
        for event in observation.events
        if event.sample_candidate_reason.strip()
    )
    # Model candidate counts vary run to run (live: 1..3 of 5 events).
    # Top up deterministically from the SAME observation's event
    # boundaries so the operator still gets up to 3 distinct places.
    if len(candidates) < MAX_SAMPLE_CANDIDATES:
        mids = [(c.start_second + c.end_second) / 2.0 for c in candidates]
        topped = list(candidates)
        for index, event in enumerate(observation.events):
            if len(topped) >= MAX_SAMPLE_CANDIDATES:
                break
            mid = (event.start_seconds + event.end_seconds) / 2.0
            if all(abs(mid - m) >= _ANCHOR_SPREAD_MIN_S for m in mids):
                mids.append(mid)
                topped.append(
                    SampleCandidate(
                        start_second=max(event.start_seconds, 0.0),
                        end_second=min(event.end_seconds, duration_seconds),
                        reason=f"(補完) 観察イベント{index}の境界から決定論的に補完"
                        "(候補理由付きイベントが3件未満のため)",
                    )
                )
        candidates = tuple(topped)
    return SampleChunkObservation(
        chunk_index=chunk.index,
        chunk_start_seconds=chunk.start_seconds,
        chunk_end_seconds=chunk.end_seconds,
        findings=findings,
        candidates=candidates,
        uncertainty=observation.uncertain_flags or None,
        quality_insufficient=observation.route_quality_insufficient,
        insufficiency_note=(
            f"{len(observation.drift_flags)}/{len(observation.events)} events drifted "
            "out of bounds (clamped)"
            if observation.route_quality_insufficient
            else None
        ),
    )


def resolve_av_sample_windows(
    full_ir: TimelineIr0C, episode_dir: Path, episode_id: str
) -> tuple[list[RecordFrameSpan], str]:
    """Observe the whole edit source in ONE Gemini AV call; resolve widened
    windows plus the journal-ready observation record (JSON string)."""

    from services.analyze.audio_probe import (  # noqa: PLC0415 (pinned tools off the fast path)
        resolve_audio_tools,
    )
    from services.analyze.visual_decode import probe_video_facts  # noqa: PLC0415
    from services.media_intelligence.gemini_av_observation import (  # noqa: PLC0415
        observe_whole_video_av,
    )
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
    observation = observe_whole_video_av(
        transport,
        pin=pin,
        env=env,
        episode_dir=episode_dir,
        episode_id=episode_id,
        proxy_path=proxy,
        duration_seconds=duration_seconds,
    )
    pseudo = _av_observation_to_chunk(observation, duration_seconds)
    whole = (SampleChunk(index=0, start_seconds=0.0, end_seconds=duration_seconds),)
    anchors = pick_candidate_anchors((pseudo,))
    record = observation_record(whole, (pseudo,), anchors)
    record["route"] = "audiovisual"
    record["timestamp_drift"] = [
        {"index": flag.index, "raw_start": flag.raw_start, "raw_end": flag.raw_end}
        for flag in observation.drift_flags
    ]
    record["audio_integration"] = "confirmed"
    record["meter"] = dict(observation.meter)
    record["result_reused"] = observation.reused
    adopted = latest_adopted_policy(episode_dir)
    record["policy_text"] = (  # comparison CONDITIONS only, never commands
        adopted.model_dump_json() if adopted is not None else ""
    )
    windows = resolve_observed_windows(
        full_ir=full_ir, source_rate=rate, observations=(pseudo,)
    )
    return list(windows), json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["ensure_av_proxy", "resolve_av_sample_windows"]
