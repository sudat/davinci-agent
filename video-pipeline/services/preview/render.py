"""render_preview: low-resolution Editorial Preview MP4 plus its trace manifest.

Assembly uses integer-exact ``trim``/``atrim`` hard cuts, ``concat``, a 640x360
``scale``, a lavfi corner-marker ``overlay``, ``amix`` BGM when bound, and a
soft ``mov_text`` subtitle track; the produced file is then re-verified with
the pinned ffprobe and hashed frame-semantic-identically across renders.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.outputs.geometry import subtitle_chars_for_output
from services.preview.ffmpeg_cmd import build_render_command, preview_size_for
from services.preview.models import (
    AppliedDecision,
    MediaBinding,
    PresentationRenderSettings,
    PreviewBindingError,
    PreviewLayout,
    PreviewLayoutError,
    PreviewMediaBindings,
    PreviewRenderError,
    PreviewTraceManifest,
    TracePresentation,
)
from services.preview.srt import (
    expected_subtitle_cues,
    expected_subtitle_cues_wrapped,
    parse_srt,
    render_srt,
)
from services.preview.styling import carry_styled
from services.preview.tools import (
    FFMPEG_TIMEOUT_SECONDS,
    PinnedTools,
    decoded_video_sha256,
    probe_file,
    run_bounded,
)
from services.preview.trace import TraceContext, build_trace, plan_version_of
from services.preview.verify import AUDIO_SAMPLE_RATE_TEXT, verify_preview_output

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C, TimelineItem0C
    from services.presentation.styling_models import StyledPresentation

PREVIEW_NAME: Final = "preview.mp4"
PREVIEW_VERTICAL_NAME: Final = "preview-vertical.mp4"
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


def _plan_content_sha256(edit_plan: EditPlan0C | None) -> str | None:
    """Content sha of the rendered edit plan, or None when there is no plan.

    Uses the same canonical bytes the review store hashes at commit time
    (``sha256_bytes(canonical_model_bytes(plan))`` in
    ``services/review_command/commit.py`` and ``policy_commit.py``,
    surfaced as ``services.cli.project.plan_sha256``), so the result is
    directly comparable to the ``VersionEntry.plan_sha256`` recorded in
    the review-store ``versions.json`` chain.
    """

    if edit_plan is None:
        return None
    return hashlib.sha256(canonical_model_bytes(edit_plan)).hexdigest()


def _check_decision_plan_agreement(
    edit_plan: EditPlan0C | None, decision: AppliedDecision, plan_version: str,
) -> None:
    """Refuse a render whose decision refers to a different plan version.

    Version-drift protection: the render must use the plan the decision
    refers to. Equal version labels pass unchanged. When the two
    independent chains' counters differ, the render is still accepted if
    the decision provably refers to this exact plan content — i.e. the
    decision-side sha (the review-store ``versions.json`` entry hash for
    ``plan_version_after``) equals the rendered plan's content sha.
    A sha mismatch is a stale plan and is refused exactly as before; a
    missing sha on either side is an honest typed refusal (no guessing).
    """

    if decision.plan_version_after == plan_version:
        return
    decision_sha = decision.plan_sha256
    plan_sha = _plan_content_sha256(edit_plan)
    if decision_sha is None or plan_sha is None:
        missing = "decision" if decision_sha is None else "edit-plan"
        raise PreviewLayoutError(
            f"decision bumps to {decision.plan_version_after} but the plan is "
            f"{plan_version}: plan identity is unprovable (missing sha on the "
            f"{missing} side); refusing a possibly stale plan"
        )
    if decision_sha != plan_sha:
        raise PreviewLayoutError(
            f"decision bumps to {decision.plan_version_after} but the plan is {plan_version}"
        )


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
    layout: PreviewLayout,
    bindings: PreviewMediaBindings,
    tools: PinnedTools,
    timeout_seconds: float | None = None,
) -> None:
    rate_fraction = Fraction(layout.rate.num, layout.rate.den)
    probed: set[str] = set()
    for item in (*layout.video_items, *layout.audio_items):
        path = Path(_require_binding(bindings, item.item_id, item.kind).media_path)
        if str(path) in probed:
            continue
        probed.add(str(path))
        report = probe_file(tools, path, timeout_seconds=timeout_seconds)
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


def _verify_subtitle_binding(
    layout: PreviewLayout,
    bindings: PreviewMediaBindings,
    subtitle_wrap_chars: int | None = None,
) -> None:
    if not layout.subtitle_items:
        return
    subtitle_paths = {
        _require_binding(bindings, item.item_id, "subtitle").media_path
        for item in layout.subtitle_items
    }
    if len(subtitle_paths) != 1:
        raise PreviewBindingError("all subtitle items must bind one subtitle table")
    cues = parse_srt(Path(next(iter(subtitle_paths))).read_bytes())
    expected = (
        expected_subtitle_cues_wrapped(layout.subtitle_items, layout.rate, subtitle_wrap_chars)
        if subtitle_wrap_chars is not None
        else expected_subtitle_cues(layout.subtitle_items, layout.rate)
    )
    if cues != expected:
        raise PreviewBindingError(
            "bound subtitle table does not match the IR subtitle items "
            "(record-coordinate shift required)"
        )


def preview_name_for(output_id: str) -> str:
    """Preview file name per output (landscape keeps the LEGACY preview.mp4)."""
    if output_id == "vertical":
        return PREVIEW_VERTICAL_NAME
    return PREVIEW_NAME


def render_preview(  # noqa: PLR0913 (brief-mandated adapter signature)
    edit_plan: EditPlan0C | None,
    timeline_ir: TimelineIr0C,
    media_bindings: PreviewMediaBindings,
    out_dir: Path,
    *,
    tools: PinnedTools,
    decision: AppliedDecision | None = None,
    styled: StyledPresentation | None = None,
    timeout_seconds: float | None = None,
    output_id: str = "landscape",
    presentation: PresentationRenderSettings | None = None,
    presentation_trace: TracePresentation | None = None,
) -> PreviewTraceManifest:
    tools.verify_current()
    plan_version = plan_version_of(edit_plan)
    if edit_plan is not None:
        _assert_plan_agreement(edit_plan, timeline_ir)
    if decision is not None:
        _check_decision_plan_agreement(edit_plan, decision, plan_version)
    layout = extract_layout(timeline_ir)
    style_table = carry_styled(layout, styled) if styled is not None else None
    subtitle_wrap_chars: int | None = None
    if presentation is not None and presentation.subtitle_max_chars_per_line is not None:
        subtitle_wrap_chars = subtitle_chars_for_output(
            presentation.subtitle_max_chars_per_line,
            "vertical" if output_id == "vertical" else "landscape",
        )
    for item in (*layout.video_items, *layout.audio_items, *layout.subtitle_items):
        _require_binding(media_bindings, item.item_id, item.kind)
    _verify_av_media(layout, media_bindings, tools, timeout_seconds)
    _verify_subtitle_binding(layout, media_bindings, subtitle_wrap_chars)
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_width, preview_height = preview_size_for(output_id)
    output = out_dir / preview_name_for(output_id)
    generated_srt: Path | None = None
    if layout.subtitle_items:
        if subtitle_wrap_chars is not None:
            cues = expected_subtitle_cues_wrapped(
                layout.subtitle_items, layout.rate, subtitle_wrap_chars
            )
        else:
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
            preview_width=preview_width,
            preview_height=preview_height,
            bgm_gain_mb=presentation.bgm_gain_mb if presentation is not None else None,
        )
        render_timeout = (
            FFMPEG_TIMEOUT_SECONDS
            if timeout_seconds is None
            else min(float(FFMPEG_TIMEOUT_SECONDS), timeout_seconds)
        )
        result = run_bounded(list(command.argv), timeout_seconds=render_timeout)
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
            timeout_seconds=timeout_seconds,
            expected_width=preview_width,
            expected_height=preview_height,
        )
        context = TraceContext(
            ir=timeline_ir,
            layout=layout,
            bindings=media_bindings,
            plan_version=plan_version,
            decision=decision,
            styled=style_table,
            output_id="vertical" if output_id == "vertical" else "landscape",
            presentation=presentation_trace,
        )
        manifest = build_trace(
            context, output, summary,
            decoded_video_sha256(tools, output, timeout_seconds=timeout_seconds),
        )
        atomic_write(out_dir / TRACE_NAME, canonical_model_bytes(manifest))
        return manifest
    finally:
        if generated_srt is not None:
            generated_srt.unlink(missing_ok=True)


__all__ = [
    "PREVIEW_NAME",
    "PREVIEW_VERTICAL_NAME",
    "TRACE_NAME",
    "extract_layout",
    "preview_name_for",
    "render_preview",
]
