"""render_preview: low-resolution Editorial Preview MP4 plus its trace manifest.

Assembly uses integer-exact ``trim``/``atrim`` hard cuts, ``concat``, a 640x360
``scale``, a lavfi corner-marker ``overlay``, ``amix`` BGM when bound, and a
soft ``mov_text`` subtitle track; the produced file is then re-verified with
the pinned ffprobe and hashed frame-semantic-identically across renders.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.ffmpeg_cmd import build_render_command
from services.preview.models import (
    AppliedDecision,
    MediaBinding,
    PreviewBindingError,
    PreviewLayout,
    PreviewLayoutError,
    PreviewMediaBindings,
    PreviewRenderError,
    PreviewTraceManifest,
)
from services.preview.srt import expected_subtitle_cues, parse_srt, render_srt
from services.preview.tools import PinnedTools, decoded_video_sha256, probe_file, run_bounded
from services.preview.trace import TraceContext, build_trace
from services.preview.verify import AUDIO_SAMPLE_RATE_TEXT, verify_preview_output

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C, TimelineItem0C

PREVIEW_NAME: Final = "preview.mp4"
TRACE_NAME: Final = "preview-trace.json"
AUDIO_SAMPLE_RATE_HZ: Final = 48000
GENERATED_SRT_NAME: Final = "preview-subtitles.generated.srt"


def _tiling(items: tuple[TimelineItem0C, ...], label: str) -> int:
    cursor = 0
    for item in items:
        span = item.record_span
        if span.end_frame <= span.start_frame:
            raise PreviewLayoutError(f"{label} item {item.item_id} has an empty record span")
        if span.start_frame != cursor:
            raise PreviewLayoutError(
                f"{label} track has a gap/overlap at {span.start_frame}; expected {cursor}"
            )
        cursor = span.end_frame
    return cursor


def extract_layout(ir: TimelineIr0C) -> PreviewLayout:
    video_tracks = [track for track in ir.tracks if track.track.kind == "video"]
    audio_tracks = [track for track in ir.tracks if track.track.kind == "audio"]
    subtitle_tracks = [track for track in ir.tracks if track.track.kind == "subtitle"]
    if len(video_tracks) != 1 or len(audio_tracks) != 1 or len(subtitle_tracks) > 1:
        raise PreviewLayoutError(
            "preview requires exactly one video track, one audio track, "
            f"and at most one subtitle track; found {len(video_tracks)}/"
            f"{len(audio_tracks)}/{len(subtitle_tracks)}"
        )
    video = video_tracks[0].items
    audio = audio_tracks[0].items
    subtitles = subtitle_tracks[0].items if subtitle_tracks else ()
    total = _tiling(video, "video")
    audio_total = _tiling(audio, "audio")
    if total != audio_total:
        raise PreviewLayoutError(
            f"video extent {total} != audio extent {audio_total}; preview requires linked A/V"
        )
    for item in subtitles:
        span = item.record_span
        if span.end_frame <= span.start_frame or span.start_frame < 0 or span.end_frame > total:
            raise PreviewLayoutError(
                f"subtitle item {item.item_id} falls outside the record extent [0,{total})"
            )
    rate = ir.rate
    samples_per_frame = Fraction(AUDIO_SAMPLE_RATE_HZ * rate.den, rate.num)
    if samples_per_frame.denominator != 1:
        raise PreviewLayoutError(
            f"frame rate {rate.num}/{rate.den} has a non-integer sample count at 48 kHz"
        )
    return PreviewLayout(
        rate=rate,
        video_items=video,
        audio_items=audio,
        subtitle_items=subtitles,
        total_record_frames=total,
        samples_per_frame=int(samples_per_frame),
    )


def _assert_plan_agreement(plan: EditPlan0C, ir: TimelineIr0C) -> None:
    if plan.frame_rate != ir.rate:
        raise PreviewLayoutError("timeline IR rate does not match the edit plan frame rate")
    plan_video = {
        item.item_id: (item.span.start_frame, item.span.end_frame)
        for item in plan.plan.items
        if item.kind == "video"
    }
    ir_video = {
        item.item_id: (item.source.span.start_frame, item.source.span.end_frame)
        for track in ir.tracks
        if track.track.kind == "video"
        for item in track.items
    }
    if plan_video != ir_video:
        raise PreviewLayoutError("timeline IR video items do not match the edit plan")


def _plan_version(edit_plan: EditPlan0C | None) -> str:
    return edit_plan.plan.plan_version if edit_plan is not None else "v1"


def _require_binding(bindings: PreviewMediaBindings, item_id: str, kind: str) -> MediaBinding:
    binding = bindings.binding_for(item_id)
    if binding is None:
        raise PreviewBindingError(f"missing media binding for {kind} item {item_id}")
    path = Path(binding.media_path)
    if not path.is_file():
        raise PreviewBindingError(f"bound media file missing: {path}")
    if sha256_file(path) != binding.sha256:
        raise PreviewBindingError(f"media sha256 drift for {item_id}: {path}")
    return binding


def _verify_av_media(
    layout: PreviewLayout, bindings: PreviewMediaBindings, tools: PinnedTools
) -> None:
    rate_fraction = Fraction(layout.rate.num, layout.rate.den)
    probed: set[str] = set()
    for item in (*layout.video_items, *layout.audio_items):
        path = Path(_require_binding(bindings, item.item_id, item.kind).media_path)
        if str(path) in probed:
            continue
        probed.add(str(path))
        report = probe_file(tools, path)
        video = next((s for s in report.streams if s.codec_type == "video"), None)
        if video is not None and Fraction(video.r_frame_rate or "0/1") != rate_fraction:
            raise PreviewBindingError(
                f"media {path} is {video.r_frame_rate}, not the timeline rate "
                f"{layout.rate.num}/{layout.rate.den}"
            )
        audio = next((s for s in report.streams if s.codec_type == "audio"), None)
        if audio is None or audio.sample_rate != AUDIO_SAMPLE_RATE_TEXT:
            raise PreviewBindingError(f"media {path} lacks a 48 kHz audio stream")
    if bindings.bgm is not None:
        bgm = Path(bindings.bgm.media_path)
        report = probe_file(tools, bgm)
        audio = next((s for s in report.streams if s.codec_type == "audio"), None)
        if audio is None or audio.sample_rate != AUDIO_SAMPLE_RATE_TEXT:
            raise PreviewBindingError(f"bgm fixture {bgm} lacks a 48 kHz audio stream")


def _verify_subtitle_binding(layout: PreviewLayout, bindings: PreviewMediaBindings) -> None:
    if not layout.subtitle_items:
        return
    subtitle_paths = {
        _require_binding(bindings, item.item_id, "subtitle").media_path
        for item in layout.subtitle_items
    }
    if len(subtitle_paths) != 1:
        raise PreviewBindingError("all subtitle items must bind one subtitle table")
    cues = parse_srt(Path(next(iter(subtitle_paths))).read_bytes())
    if cues != expected_subtitle_cues(layout.subtitle_items, layout.rate):
        raise PreviewBindingError(
            "bound subtitle table does not match the IR subtitle items "
            "(record-coordinate shift required)"
        )


def render_preview(  # noqa: PLR0913 (brief-mandated adapter signature)
    edit_plan: EditPlan0C | None,
    timeline_ir: TimelineIr0C,
    media_bindings: PreviewMediaBindings,
    out_dir: Path,
    *,
    tools: PinnedTools,
    decision: AppliedDecision | None = None,
) -> PreviewTraceManifest:
    tools.verify_current()
    plan_version = _plan_version(edit_plan)
    if edit_plan is not None:
        _assert_plan_agreement(edit_plan, timeline_ir)
    if decision is not None and decision.plan_version_after != plan_version:
        raise PreviewLayoutError(
            f"decision bumps to {decision.plan_version_after} but the plan is {plan_version}"
        )
    layout = extract_layout(timeline_ir)
    for item in (*layout.video_items, *layout.audio_items, *layout.subtitle_items):
        _require_binding(media_bindings, item.item_id, item.kind)
    _verify_av_media(layout, media_bindings, tools)
    _verify_subtitle_binding(layout, media_bindings)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / PREVIEW_NAME
    generated_srt: Path | None = None
    if layout.subtitle_items:
        cues = expected_subtitle_cues(layout.subtitle_items, layout.rate)
        generated_srt = out_dir / GENERATED_SRT_NAME
        atomic_write(generated_srt, render_srt(cues))
    try:
        command = build_render_command(
            layout=layout,
            bindings=media_bindings,
            tools=tools,
            output=output,
            subtitle_srt=generated_srt,
        )
        result = run_bounded(list(command.argv))
        if result.returncode != 0:
            raise PreviewRenderError(
                f"ffmpeg exited {result.returncode}: {result.stderr.strip()[-1500:]}"
            )
        if not output.is_file():
            raise PreviewRenderError("ffmpeg exited 0 but the preview file is missing")
        summary = verify_preview_output(
            tools,
            output,
            total_frames=layout.total_record_frames,
            rate=timeline_ir.rate,
            subtitle_expected=bool(layout.subtitle_items),
        )
        context = TraceContext(
            ir=timeline_ir,
            layout=layout,
            bindings=media_bindings,
            plan_version=plan_version,
            decision=decision,
        )
        manifest = build_trace(context, output, summary, decoded_video_sha256(tools, output))
        atomic_write(out_dir / TRACE_NAME, canonical_model_bytes(manifest))
        return manifest
    finally:
        if generated_srt is not None:
            generated_srt.unlink(missing_ok=True)


__all__ = [
    "PREVIEW_NAME",
    "TRACE_NAME",
    "extract_layout",
    "render_preview",
]
