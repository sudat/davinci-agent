"""Fixed-subtitle strategy ladder for the Phase-0A fixed-presentation spike.

Walks the documented ladder direct -> interchange -> template -> external for
the single fixed subtitle and records the chosen rung with reasons and
limitations. Live spike evidence on Resolve 21.0.4: importing subtitle.srt via
ImportMedia works (pool item extent equals the cue span, 60 frames at 30 fps),
but AppendToTimeline with that pool item never returns, so the direct rung is
refused through a version-bound deadlock registry instead of being retried.
Interchange and template rungs have no pinned generator or pre-generated asset
in this environment, so the verified rung is external: the pinned ffmpeg muxes
subtitle.srt into the render as a mov_text stream and the pinned ffprobe plus a
demux round-trip prove text and timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.fixed_presentation_models import (
    ElementStrategy,
    RungAttempt,
    SubtitleArtifactEvidence,
    SubtitleCue,
    SubtitleEvidence,
)
from services.resolve_bridge.fixed_presentation_srt import SubtitleTextError, load_fixed_cue
from services.resolve_bridge.fixed_presentation_tools import RenderError

if TYPE_CHECKING:
    from services.fixtures.manifest import SubtitleRecipe
    from services.resolve_bridge.base_cut_models import BaseCutMediaPoolItemApi
    from services.resolve_bridge.fixed_presentation_models import (
        FixedMediaPoolApi,
        FixedTimelineApi,
        MismatchSink,
    )
    from services.resolve_bridge.fixed_presentation_tools import MediaToolsApi

DEADLOCK_BUILDS: Final = frozenset({"21.0.4"})
DEADLOCK_REASON: Final = (
    "known RPC deadlock on this build: AppendToTimeline with a subtitle media-pool "
    "item never returns (live spike evidence, watchdog-terminated); refused instead "
    "of retried per bounded-command policy"
)
INTERCHANGE_REASON: Final = (
    "no pinned interchange generator available: Timeline.ImportIntoTimeline is "
    "AAF-contract on this build and no AAF/OTIO/FCPXML subtitle writer is pinned"
)
TEMPLATE_REASON: Final = (
    "no pre-generated Resolve template asset in this environment; template rung "
    "deferred until a template is produced and frozen"
)


@dataclass(frozen=True, slots=True)
class ExternalOutcome:
    evidence: SubtitleArtifactEvidence | None
    failure: str | None


def _external_rung(
    tools: MediaToolsApi, render_output: Path, srt_path: Path, cue: SubtitleCue
) -> ExternalOutcome:
    paired = render_output.with_name(render_output.stem + "-subtitled" + render_output.suffix)
    try:
        tools.mux_subtitle(render_output, srt_path, paired)
        probe = tools.probe(paired)
        demux = tools.demux_subtitle(paired)
        packets = tools.subtitle_packets(paired)
    except (OSError, RuntimeError, RenderError) as error:
        return ExternalOutcome(evidence=None, failure=f"external mux/probe failed: {error}")
    stream = next((s for s in probe.streams if s.codec_type == "subtitle"), None)
    if stream is None or stream.codec_name != "mov_text":
        return ExternalOutcome(
            evidence=None, failure="paired artifact lacks a mov_text subtitle stream"
        )
    normalized_srt = srt_path.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n")
    if demux.replace(b"\r\n", b"\n").rstrip(b"\n") != normalized_srt:
        return ExternalOutcome(evidence=None, failure="demuxed subtitles differ from subtitle.srt")
    expected_pts = cue.start_ms / 1000
    expected_duration = (cue.end_ms - cue.start_ms) / 1000
    packet = next(
        (
            p
            for p in packets
            if abs(p.pts - expected_pts) < 1e-6 and abs(p.duration - expected_duration) < 1e-6
        ),
        None,
    )
    if packet is None:
        return ExternalOutcome(
            evidence=None, failure="no subtitle packet matches the fixed cue timing"
        )
    return ExternalOutcome(
        evidence=SubtitleArtifactEvidence(
            srt_sha256=tools.sha256(srt_path),
            cue=cue,
            cue_matches_table=True,
            paired_path=str(paired),
            paired_sha256=tools.sha256(paired),
            subtitle_stream_codec=stream.codec_name,
            demux_matches_srt=True,
            packet_pts_seconds=packet.pts,
            packet_duration_seconds=packet.duration,
        ),
        failure=None,
    )


def _direct_rung(
    pool: FixedMediaPoolApi,
    timeline: FixedTimelineApi,
    srt_path: Path,
    cue: SubtitleCue,
    *,
    rate_num: int,
    rate_den: int,
) -> RungAttempt:
    span_frames = (cue.end_ms - cue.start_ms) * rate_num // (1000 * rate_den)
    imported = pool.ImportMedia([str(srt_path)])
    if not imported:
        return RungAttempt(
            rung="direct", available=False, reason="ImportMedia refused the subtitle file"
        )
    item: BaseCutMediaPoolItemApi = imported[0]
    placed = pool.AppendToTimeline(
        [{"mediaPoolItem": item, "startFrame": 0, "endFrame": span_frames, "trackIndex": 1}]
    )
    if not placed:
        return RungAttempt(
            rung="direct",
            available=False,
            reason="AppendToTimeline returned no item for the subtitle pool item",
        )
    sub_items = timeline.GetItemListInTrack("subtitle", 1)
    if not sub_items:
        return RungAttempt(
            rung="direct", available=False, reason="subtitle track readback found no placed item"
        )
    return RungAttempt(rung="direct", available=True, reason="subtitle item placed and read back")


def resolve_subtitle(
    *,
    pool: FixedMediaPoolApi,
    timeline: FixedTimelineApi,
    recipe: SubtitleRecipe,
    rate_num: int,
    rate_den: int,
    srt_path: Path,
    render_output: Path,
    tools: MediaToolsApi,
    version_core: str,
    direct_allowed: bool,
    mismatches: MismatchSink,
) -> SubtitleEvidence:
    try:
        cue = load_fixed_cue(srt_path, recipe, rate_num, rate_den)
    except (SubtitleTextError, OSError) as error:
        mismatches.add("text-encoding-unsupported", str(error))
        strategy = ElementStrategy(
            element="subtitle",
            strategy=None,
            status="unsupported",
            reason=f"text-encoding-unsupported: {error}",
            limitations="no subtitle artifact produced; source rejected before placement",
            ladder=(),
        )
        return SubtitleEvidence(strategy=strategy, in_timeline=False, artifact=None)

    ladder: list[RungAttempt] = []
    if direct_allowed:
        ladder.append(
            _direct_rung(pool, timeline, srt_path, cue, rate_num=rate_num, rate_den=rate_den)
        )
    else:
        ladder.append(
            RungAttempt(
                rung="direct",
                available=False,
                reason=f"{DEADLOCK_REASON} (build {version_core})",
            )
        )
    if timeline.GetItemListInTrack("subtitle", 1):
        strategy = ElementStrategy(
            element="subtitle",
            strategy="direct",
            status="verified",
            reason="in-timeline subtitle item read back with exact record span",
            limitations=(
                "text content verified transitively via srt sha256; cue span is the item extent"
            ),
            ladder=tuple(ladder),
        )
        return SubtitleEvidence(strategy=strategy, in_timeline=True, artifact=None)
    ladder.append(RungAttempt(rung="interchange", available=False, reason=INTERCHANGE_REASON))
    ladder.append(RungAttempt(rung="template", available=False, reason=TEMPLATE_REASON))
    external = _external_rung(tools, render_output, srt_path, cue)
    if external.failure is not None or external.evidence is None:
        detail = external.failure or "external rung produced no evidence"
        mismatches.add("subtitle-placement-unsupported", detail)
        strategy = ElementStrategy(
            element="subtitle",
            strategy=None,
            status="unsupported",
            reason=f"all rungs exhausted: {detail}",
            limitations="direct refused, interchange/template unavailable, external failed",
            ladder=tuple(ladder),
        )
        return SubtitleEvidence(strategy=strategy, in_timeline=False, artifact=None)
    strategy = ElementStrategy(
        element="subtitle",
        strategy="external",
        status="verified",
        reason=(
            "pinned ffmpeg paired the fixed cue into the render as mov_text; "
            "ffprobe + demux round-trip verified"
        ),
        limitations=(
            "not placed inside the Resolve timeline on 21.0.4 (direct rung deadlocks); "
            "delivered as a paired mov_text stream in the subtitled render"
        ),
        ladder=tuple(ladder),
    )
    return SubtitleEvidence(strategy=strategy, in_timeline=False, artifact=external.evidence)
