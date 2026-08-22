# allow: SIZE_OK — task 60 pins the renderer to v2_editorial.py (models and
# ffmpeg argv already split into v2_models.py / v2_ffmpeg_cmd.py); the rest is
# the irreducible bind->render->trace orchestration. T31/T35 SIZE_OK precedent.

"""Editorial Preview v2: Timeline IR v2 -> rough MP4 + PreviewTraceV2 (PRD 13.1).

Content contract at documented rough fidelity (see :class:`FidelityNotesV2`):
selected clips in story order (primary track concat from the source media
map), rough B-roll/insert cut-ins at their record spans, approximate
subtitles (SRT sidecar + soft ``mov_text`` track; the pinned ffmpeg has no
libass/drawtext so nothing is burned), placeholder title cards, placeholder
sine tones for music/ambience/sfx placements, and Moment Deep Review flags —
``manual_required`` effect intents surface as manifest flags plus a burned
red corner marker.

The v1 preview path (talking-head 0C regression) is untouched; this module
is additive and Resolve-free by contract (never imports resolve_bridge).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.errors import (
    PreviewBindingError,
    PreviewLayoutError,
    PreviewRenderError,
)
from services.preview.ffmpeg_cmd import AUDIO_SAMPLE_RATE_HZ
from services.preview.srt import SubtitleCue, cue_from_record_span, render_srt
from services.preview.tools import PinnedTools, decoded_video_sha256, probe_file, run_bounded
from services.preview.v2_ffmpeg_cmd import build_editorial_command
from services.preview.v2_models import (
    TONE_HZ_BY_ROLE,
    AudioPlaceholderV2,
    AudioSegmentView,
    CardView,
    ClipView,
    CutInRecordV2,
    EditorialLayoutV2,
    FidelityNotesV2,
    MediaAudioView,
    PreviewFileV2,
    PreviewFlagV2,
    PreviewTraceV2,
    SourceMediaMapV2,
    SubtitleSidecarV2,
    ToneAudioView,
)
from services.preview.verify import verify_preview_output

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import (
        AudioItemV2,
        PlacedClipV2,
        TimelineIrV2,
        VideoTrackV2,
    )
    from services.creative_plan.subtitle_models import SubtitlePlanV1
    from services.preview.models import FfprobeSummary

SUPPORTED_VIDEO_ROLES: Final[frozenset[str]] = frozenset(
    {"primary", "b_roll", "insert", "still", "graphic"}
)
SUPPORTED_AUDIO_ROLES: Final[frozenset[str]] = frozenset({"dialogue", "music", "ambience", "sfx"})
MEDIA_CUT_IN_ROLES: Final[frozenset[str]] = frozenset({"b_roll", "insert"})
CARD_ROLES: Final[frozenset[str]] = frozenset({"still", "graphic"})
TONE_ROLES: Final[frozenset[str]] = frozenset(TONE_HZ_BY_ROLE)
DEFAULT_FLAG_REASON: Final = "manual-required-deep-review"
SIDECAR_SUFFIX: Final = ".srt"
TRACE_SUFFIX: Final = ".trace.json"


@dataclass(frozen=True, slots=True)
class _RenderOutputs:
    preview: Path
    sidecar: Path | None
    cue_count: int


def _record_span(item: PlacedClipV2 | AudioItemV2, role: str) -> tuple[int, int]:
    span = item.record_span
    if span.end_frame <= span.start_frame:
        raise PreviewLayoutError(f"{role} item {item.item_id} has an empty record span")
    return (span.start_frame, span.end_frame)


def _require_within(span: tuple[int, int], total: int, label: str, item_id: str) -> None:
    if span[0] < 0 or span[1] > total:
        raise PreviewLayoutError(f"{label} {item_id} falls outside the record extent [0,{total})")


def _flags_from_effects(ir: TimelineIrV2, item_starts: dict[str, int]) -> tuple[PreviewFlagV2, ...]:
    flags: list[PreviewFlagV2] = []
    for effect in ir.effect_intents:
        if effect.kind != "manual_required":
            continue
        if effect.target_item_id is None:
            at_frame = 0
        else:
            at_frame = item_starts.get(effect.target_item_id, -1)
            if at_frame < 0:
                raise PreviewLayoutError(
                    f"manual_required effect {effect.effect_id} targets unknown item "
                    f"{effect.target_item_id}"
                )
        flags.append(
            PreviewFlagV2(
                at_frame=at_frame,
                review_ref=effect.effect_id,
                reason=effect.note or DEFAULT_FLAG_REASON,
            )
        )
    return tuple(flags)


def _require_entry(media: SourceMediaMapV2, source_id: str, item_id: str) -> Path:
    entry = media.entry_for(source_id)
    if entry is None:
        raise PreviewBindingError(f"missing media binding for item {item_id} (source {source_id})")
    path = Path(entry.media_path)
    if not path.is_file():
        raise PreviewBindingError(f"bound media file missing: {path}")
    if sha256_file(path) != entry.sha256:
        raise PreviewBindingError(f"media sha256 drift for {item_id}: {path}")
    return path


def _verify_media_streams(tools: PinnedTools, paths: tuple[Path, ...], rate_text: str) -> None:
    probed: set[str] = set()
    for path in paths:
        if str(path) in probed:
            continue
        probed.add(str(path))
        report = probe_file(tools, path)
        video = next((s for s in report.streams if s.codec_type == "video"), None)
        audio = next((s for s in report.streams if s.codec_type == "audio"), None)
        if video is None or video.r_frame_rate != rate_text:
            raise PreviewBindingError(f"media {path} lacks a {rate_text} video stream")
        if audio is None or audio.sample_rate != "48000":
            raise PreviewBindingError(f"media {path} lacks a 48 kHz audio stream")


def _sidecar_cues(
    ir: TimelineIrV2, subtitle_plan: SubtitlePlanV1 | None
) -> tuple[SubtitleCue, ...]:
    if subtitle_plan is not None:
        cues = [
            cue_from_record_span(cue.record_span, ir.rate, "\n".join(cue.lines))
            for cue in subtitle_plan.cues
        ]
    else:
        cues = [
            cue_from_record_span(cue.record_span, ir.rate, cue.text) for cue in ir.subtitle_cues
        ]
    return tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.text)))


def _primary_track_and_extent(primary_track: VideoTrackV2) -> tuple[tuple[int, int], ...]:
    spans = [_record_span(item, "primary") for item in primary_track.items]
    if spans[0][0] != 0:
        raise PreviewLayoutError("primary must start at record frame 0")
    for earlier, later in pairwise(spans):
        if later[0] != earlier[1]:
            raise PreviewLayoutError(
                f"primary gap/overlap at record frame {later[0]}; expected {earlier[1]}"
            )
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class _VideoBindings:
    primary: tuple[ClipView, ...]
    media_cut_ins: tuple[ClipView, ...]
    card_views: tuple[CardView, ...]
    cut_in_records: tuple[CutInRecordV2, ...]


def _bind_video_tracks(ir: TimelineIrV2, media: SourceMediaMapV2, total: int) -> _VideoBindings:
    primary: list[ClipView] = []
    primary_track = next(track for track in ir.video_tracks if track.role == "primary")
    for item, span in zip(
        primary_track.items, _primary_track_and_extent(primary_track), strict=True
    ):
        primary.append(
            ClipView(
                item_id=item.item_id,
                media_path=_require_entry(media, item.source.source_id, item.item_id),
                source_span=(item.source.span.start_frame, item.source.span.end_frame),
                record_span=span,
            )
        )

    cut_in_views: list[ClipView] = []
    cut_in_records: list[CutInRecordV2] = []
    card_views: list[CardView] = []
    for track in sorted(ir.video_tracks, key=lambda entry: (entry.role, entry.track_id)):
        match track.role:
            case "primary":
                continue
            case "b_roll" | "insert" as role:
                for item in track.items:
                    span = _record_span(item, role)
                    _require_within(span, total, role, item.item_id)
                    cut_in_views.append(
                        ClipView(
                            item_id=item.item_id,
                            media_path=_require_entry(media, item.source.source_id, item.item_id),
                            source_span=(
                                item.source.span.start_frame,
                                item.source.span.end_frame,
                            ),
                            record_span=span,
                        )
                    )
                    cut_in_records.append(
                        CutInRecordV2(
                            item_id=item.item_id, role=role, start_frame=span[0], end_frame=span[1]
                        )
                    )
            case "still" | "graphic" as role:
                for item in track.items:
                    span = _record_span(item, role)
                    _require_within(span, total, role, item.item_id)
                    card_views.append(CardView(item_id=item.item_id, record_span=span))

    ordered = sorted(
        zip(cut_in_views, cut_in_records, strict=True),
        key=lambda pair: (pair[0].record_span[0], pair[0].item_id),
    )
    return _VideoBindings(
        primary=tuple(primary),
        media_cut_ins=tuple(view for view, _ in ordered),
        card_views=tuple(sorted(card_views, key=lambda card: (card.record_span[0], card.item_id))),
        cut_in_records=tuple(record for _, record in ordered),
    )


@dataclass(frozen=True, slots=True)
class _AudioBindings:
    segments: tuple[AudioSegmentView, ...]
    placeholders: tuple[AudioPlaceholderV2, ...]


def _bind_audio_tracks(
    ir: TimelineIrV2, media: SourceMediaMapV2, total: int, per_frame: int
) -> _AudioBindings:
    segments: list[AudioSegmentView] = []
    placeholders: list[AudioPlaceholderV2] = []
    for track in sorted(ir.audio_tracks, key=lambda entry: (entry.role, entry.track_id)):
        match track.role:
            case "dialogue":
                for item in track.items:
                    span = _record_span(item, "dialogue")
                    _require_within(span, total, "dialogue", item.item_id)
                    segments.append(
                        MediaAudioView(
                            item_id=item.item_id,
                            media_path=_require_entry(media, item.source.source_id, item.item_id),
                            source_start_sample=item.source.span.start_frame * per_frame,
                            source_end_sample=item.source.span.end_frame * per_frame,
                            delay_samples=span[0] * per_frame,
                        )
                    )
            case "music" | "ambience" | "sfx" as role:
                hz = TONE_HZ_BY_ROLE[role]
                for item in track.items:
                    span = _record_span(item, role)
                    _require_within(span, total, role, item.item_id)
                    placeholders.append(
                        AudioPlaceholderV2(
                            item_id=item.item_id,
                            role=role,
                            frequency_hz=hz,
                            start_frame=span[0],
                            end_frame=span[1],
                        )
                    )
                    segments.append(
                        ToneAudioView(
                            item_id=item.item_id,
                            tone_hz=hz,
                            length_samples=(span[1] - span[0]) * per_frame,
                            delay_samples=span[0] * per_frame,
                        )
                    )
    return _AudioBindings(segments=tuple(segments), placeholders=tuple(placeholders))


def extract_editorial_layout(
    ir: TimelineIrV2, media: SourceMediaMapV2, *, tools: PinnedTools
) -> EditorialLayoutV2:
    """Validate + media-bind a Timeline IR v2 into a render-ready layout.

    Unknown track roles are typed errors (never silent skips); the role guard
    runs before any layout math so a future IR role literal fails loudly at
    this renderer first.
    """

    for track in ir.video_tracks:
        if track.role not in SUPPORTED_VIDEO_ROLES:
            raise PreviewLayoutError(f"unsupported video track role: {track.role}")
    for track in ir.audio_tracks:
        if track.role not in SUPPORTED_AUDIO_ROLES:
            raise PreviewLayoutError(f"unsupported audio track role: {track.role}")
    primary_track = next((t for t in ir.video_tracks if t.role == "primary"), None)
    if primary_track is None:
        raise PreviewLayoutError("editorial preview requires a primary video track")
    total = _primary_track_and_extent(primary_track)[-1][1]

    rate = ir.rate
    samples_per_frame = Fraction(AUDIO_SAMPLE_RATE_HZ * rate.den, rate.num)
    if samples_per_frame.denominator != 1:
        raise PreviewLayoutError(
            f"frame rate {rate.num}/{rate.den} has a non-integer sample count at 48 kHz"
        )
    per_frame = int(samples_per_frame)

    video = _bind_video_tracks(ir, media, total)
    audio = _bind_audio_tracks(ir, media, total, per_frame)

    item_starts = {
        item.item_id: _record_span(item, "video")[0]
        for track in ir.video_tracks
        for item in track.items
    }
    item_starts.update(
        {
            item.item_id: _record_span(item, "audio")[0]
            for track in ir.audio_tracks
            for item in track.items
        }
    )

    media_paths = {str(view.media_path): view.media_path for view in video.primary}
    media_paths.update({str(view.media_path): view.media_path for view in video.media_cut_ins})
    for segment in audio.segments:
        if isinstance(segment, MediaAudioView):
            media_paths[str(segment.media_path)] = segment.media_path
    _verify_media_streams(tools, tuple(media_paths.values()), f"{rate.num}/{rate.den}")

    return EditorialLayoutV2(
        rate=rate,
        samples_per_frame=per_frame,
        total_record_frames=total,
        primary=video.primary,
        media_cut_ins=video.media_cut_ins,
        card_views=video.card_views,
        audio_segments=audio.segments,
        cut_in_records=video.cut_in_records,
        audio_placeholders=audio.placeholders,
        flags=_flags_from_effects(ir, item_starts),
    )


def render_editorial_preview(
    ir_v2: TimelineIrV2,
    *,
    subtitle_plan: SubtitlePlanV1 | None,
    source_media_map: SourceMediaMapV2,
    output_path: Path,
    tools: PinnedTools,
) -> PreviewTraceV2:
    """Render the Editorial Preview MP4 + SRT sidecar + trace manifest."""

    tools.verify_current()
    if subtitle_plan is not None:
        if subtitle_plan.episode_id != ir_v2.episode_id:
            raise PreviewBindingError(
                f"subtitle_plan episode {subtitle_plan.episode_id} does not match the IR "
                f"episode {ir_v2.episode_id}"
            )
        if subtitle_plan.rate != ir_v2.rate:
            raise PreviewBindingError("subtitle_plan rate does not match the IR rate")
    layout = extract_editorial_layout(ir_v2, source_media_map, tools=tools)

    cues = _sidecar_cues(ir_v2, subtitle_plan)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path = output_path.with_suffix(SIDECAR_SUFFIX)
    if cues:
        atomic_write(sidecar_path, render_srt(cues))
    command = build_editorial_command(
        layout=layout,
        tools=tools,
        output=output_path,
        subtitle_srt=sidecar_path if cues else None,
    )
    result = run_bounded(list(command.argv))
    if result.returncode != 0:
        raise PreviewRenderError(
            f"ffmpeg exited {result.returncode}: {result.stderr.strip()[-1500:]}"
        )
    if not output_path.is_file():
        raise PreviewRenderError("ffmpeg exited 0 but the preview file is missing")
    summary = verify_preview_output(
        tools,
        output_path,
        total_frames=layout.total_record_frames,
        rate=ir_v2.rate,
        subtitle_expected=bool(cues),
    )
    outputs = _RenderOutputs(
        preview=output_path,
        sidecar=sidecar_path if cues else None,
        cue_count=len(cues),
    )
    trace = _build_trace(ir_v2, layout, outputs, summary, tools)
    atomic_write(
        output_path.with_name(output_path.stem + TRACE_SUFFIX), canonical_model_bytes(trace)
    )
    return trace


def _build_trace(
    ir: TimelineIrV2,
    layout: EditorialLayoutV2,
    outputs: _RenderOutputs,
    summary: FfprobeSummary,
    tools: PinnedTools,
) -> PreviewTraceV2:
    sidecar = (
        SubtitleSidecarV2(
            path=str(outputs.sidecar),
            sha256=sha256_file(outputs.sidecar),
            size=outputs.sidecar.stat().st_size,
            cue_count=outputs.cue_count,
        )
        if outputs.sidecar is not None
        else None
    )
    return PreviewTraceV2(
        schema_version="preview-trace-v2",
        episode_id=ir.episode_id,
        ir_sha256=hashlib.sha256(canonical_model_bytes(ir)).hexdigest(),
        rate=ir.rate,
        total_record_frames=layout.total_record_frames,
        preview=PreviewFileV2(
            path=str(outputs.preview),
            sha256=sha256_file(outputs.preview),
            size=outputs.preview.stat().st_size,
            decoded_video_sha256=decoded_video_sha256(tools, outputs.preview),
        ),
        subtitle_sidecar=sidecar,
        flags=layout.flags,
        cut_ins=layout.cut_in_records,
        audio_placeholders=layout.audio_placeholders,
        fidelity=FidelityNotesV2(
            b_roll="full-frame-cut-in-overlay-pm1-frame-boundary",
            subtitles="srt-sidecar-plus-soft-mov-text-no-burn-in",
            titles="white-placeholder-card-no-burned-text",
            music_ambience="sine-tone-placeholders-music440-ambience220-sfx880",
            flags="manifest-plus-red-corner-marker-overlay",
            determinism_policy="semantic-equivalence-h264-videotoolbox",
        ),
        ffprobe_summary=summary,
    )


__all__ = [
    "SUPPORTED_AUDIO_ROLES",
    "SUPPORTED_VIDEO_ROLES",
    "extract_editorial_layout",
    "render_editorial_preview",
]
