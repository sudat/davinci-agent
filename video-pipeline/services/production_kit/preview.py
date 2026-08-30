"""Production-kit recipe A/B previews over REAL episode footage (task 11).

``plan_previews`` renders one snippet of the episode's edit source through
the EXISTING pinned-ffmpeg preview renderer (``services.preview.render``)
for every candidate recipe in each presentation domain, so the operator
can A/B the channel kit on real footage before the Resolve finishing
build consumes the recorded choice.

Snippet regions come from the episode's committed state: the v2
media-intelligence index (``best_moments``/``shots``, T8's index
convention) picks a representative 10-20 s window over the CFR mezzanine;
fixture episodes pass explicit :class:`SnippetSpecV1` regions instead. A
missing committed selection store is a typed refusal — kit previews are
post-``PLAN_COMMITTED`` by definition.

Per-domain resolved-param application table (NO new recipe families; the
12 kit recipes stay 12):

=================  ==========================  ===============================
domain             param                       application in the snippet mp4
=================  ==========================  ===============================
subtitle           ``line_length``             APPLIED: subtitle cue text is
                                               wrapped at N chars when the
                                               minimal snippet IR is built
                                               (visible in the soft mov_text
                                               track).
subtitle           ``font_size``               RECORDED in the manifest; the
subtitle           ``emphasis_scale``          pinned soft-sub muxer carries
subtitle           ``hold_frames``             no burn-in styling (Todo-57
                                               precedent: pixel content is
                                               untouched by styling) — the
                                               Resolve build applies them.
audio              ``target_lufs``             RECORDED; applied by the
audio              ``noise_reduction``         Resolve finishing build
audio              ``duck_level_db``           (``set_voice_isolation_state``
audio              ``attack_frames``           / ``safe_set_audio_properties``
                                               step surfaces).
color              ``exposure_compensation``   RECORDED; applied by the
color              ``white_balance_shift``     Resolve finishing build
color              ``look_strength``           (``safe_apply_drx`` grade
color              ``saturation``              steps).
=================  ==========================  ===============================

Params a recipe does not declare are never invented (task-37 discipline:
``apply_taste_params`` is the only resolved-params path). The operator's
A/B choice lands in ``kit-selections.json`` (:class:`KitSelectionRecordV1`,
schema ``kit-selection-v1`` — a RUNTIME selection record, NOT a new
authoritative artifact): the record round-trips through
``recipe_selection_from_record`` back to the same
:class:`~services.production_kit.recipe_select.RecipeSelection` the
finishing build would resolve.

Kit-preview subtitle burn-in (fix for operator report "字幕もないし"):
the main pipeline preview keeps a soft ``mov_text`` track (PRD design),
but kit-preview SUBTITLE domain burns the cues into pixels via an extra
pinned-ffmpeg overlay pass so the operator can visually compare font sizes
(``font_size`` for default; ``emphasis_scale`` for emphasis) and wrapping
(``line_length`` already applied to cue text). Audio/color domains keep
soft/no subs. The burn overlay is generated with Pillow at 640x360 and
composited with the pinned ffmpeg ``overlay`` filter (the only burned
video filter the pinned binary carries). The soft ``mov_text`` track is
dropped after burning (it was never visible in the cockpit preview player
without text-track rendering).
"""

# allow: SIZE_OK — plan-pinned single-file task-11 scope (models + param
# table + snippet planner + selection record); splitting would fork the
# task-37 selection semantics this module must round-trip exactly.

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from math import ceil, floor, gcd
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
    StrictModel,
)
from services.contracts.timeline_ir import (
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
    TrackRef0C,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.models import (
    ItemBinding,
    MediaBinding,
    PreviewMediaBindings,
)
from services.preview.render import PREVIEW_NAME, render_preview
from services.preview.srt import expected_subtitle_cues, render_srt
from services.preview.tools import PinnedTools, load_pinned_tools, probe_file
from services.production_kit.models import (
    ChannelProductionKitV1,
    ParameterBoundV1,
)
from services.production_kit.recipe_select import RecipeSelection, select_recipe

KIT_PREVIEWS_DIR: Final = "kit-previews"
MANIFEST_NAME: Final = "manifest.json"
SELECTIONS_NAME: Final = "kit-selections.json"
SELECTION_STORE_RELATIVE: Final = ("run", "selection-plan", "versions.json")
MEZZANINE_RELATIVE: Final = ("run", "media", "edit-source.mov")
INDEX_NAME: Final = "media-intelligence.duckdb"
DEFAULT_DOMAINS: Final = ("subtitle", "audio", "color")
MIN_SNIPPET_SECONDS: Final = Fraction(10)
MAX_SNIPPET_SECONDS: Final = Fraction(20)
PREVIEW_PRODUCER: Final = Producer(name="production-kit-preview", version="1")
SRT_WORK_NAME: Final = "snippet.srt"


class KitPreviewError(Exception):
    """Typed kit-preview refusal (code + detail; never a bare string)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Strict models — snippet spec, manifest, runtime selection record
# ---------------------------------------------------------------------------


class SnippetSpecV1(StrictModel):
    """Explicit snippet region over one fixture medium (fixture-episode path)."""

    media_path: str = Field(min_length=1, strict=True)
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)
    subtitle_text: str | None = None

    @model_validator(mode="after")
    def require_forward_absolute(self) -> SnippetSpecV1:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("snippet_span", "end_frame must exceed start_frame")
        if not Path(self.media_path).is_absolute():
            raise PydanticCustomError("snippet_path", "snippet media must be an absolute path")
        return self


class KitSnippetInfoV1(StrictModel):
    """The real-footage region every candidate of one domain renders over."""

    origin: Literal["best_moment", "shot", "explicit"]
    media_path: str = Field(min_length=1, strict=True)
    media_sha256: str = Field(min_length=1, strict=True)
    rate: RationalFrameRate
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)
    duration_seconds: float = Field(gt=0.0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> KitSnippetInfoV1:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("snippet_span", "end must exceed start")
        return self


class KitPreviewCandidateV1(StrictModel):
    """One rendered candidate: file + resolved params + their bounds.

    The bounds validator IS the task-11 clipping guarantee: a manifest
    whose resolved params fall outside the recipe bounds cannot validate,
    so a tampered or stale manifest fails on read instead of on render.
    """

    recipe_id: str = Field(min_length=1, strict=True)
    semantic_intent: str = Field(min_length=1, strict=True)
    selection_rationale: str = Field(min_length=1, strict=True)
    resolved_params: dict[str, float]
    parameter_bounds: dict[str, ParameterBoundV1]
    file: str = Field(min_length=1, strict=True)
    sha256: str = Field(min_length=1, strict=True)

    @model_validator(mode="after")
    def require_params_within_bounds(self) -> KitPreviewCandidateV1:
        for param, value in self.resolved_params.items():
            bound = self.parameter_bounds.get(param)
            if bound is None:
                raise PydanticCustomError(
                    "param_without_bound",
                    "resolved param {param} has no recorded bound",
                    {"param": param},
                )
            if not (float(bound.min) <= value <= float(bound.max)):
                raise PydanticCustomError(
                    "param_out_of_bounds",
                    "resolved param {param}={value} outside [{lo}, {hi}]",
                    {
                        "param": param,
                        "value": str(value),
                        "lo": str(bound.min),
                        "hi": str(bound.max),
                    },
                )
        return self


class KitPreviewDomainV1(StrictModel):
    """One presentation domain: snippet + its candidate renders."""

    domain: str = Field(min_length=1, strict=True)
    intents: tuple[str, ...] = Field(min_length=1)
    snippet: KitSnippetInfoV1
    candidates: tuple[KitPreviewCandidateV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_candidates(self) -> KitPreviewDomainV1:
        ids = [candidate.recipe_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_candidate", "candidate recipe ids must be unique")
        return self


class KitPreviewManifestV1(StrictModel):
    """kit-previews/manifest.json — domain -> candidates -> files -> params."""

    schema_version: Literal["kit-preview-manifest-v1"] = "kit-preview-manifest-v1"
    episode_id: Identifier
    domains: tuple[KitPreviewDomainV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_domains(self) -> KitPreviewManifestV1:
        names = [entry.domain for entry in self.domains]
        if len(set(names)) != len(names):
            raise PydanticCustomError("duplicate_domain", "domain entries must be unique")
        return self


class KitDomainSelectionV1(StrictModel):
    """One operator choice: a recipe, or none (どちらも不要/現状維持)."""

    domain: str = Field(min_length=1, strict=True)
    recipe_id: str | None = None
    semantic_intent: str | None = None
    note: str | None = None
    recorded_at: str = Field(min_length=1, strict=True)

    @model_validator(mode="after")
    def require_intent_with_recipe(self) -> KitDomainSelectionV1:
        if self.recipe_id is None and self.semantic_intent is not None:
            raise PydanticCustomError("intent_without_recipe", "a none-choice carries no intent")
        if self.recipe_id is not None and self.semantic_intent is None:
            raise PydanticCustomError("recipe_without_intent", "a recipe choice carries its intent")
        return self


class KitSelectionRecordV1(StrictModel):
    """kit-selections.json — append-only runtime record (latest per domain wins)."""

    schema_version: Literal["kit-selection-v1"] = "kit-selection-v1"
    episode_id: Identifier
    entries: tuple[KitDomainSelectionV1, ...] = ()


# ---------------------------------------------------------------------------
# Domain vocabulary (derived from the kit, never a second hand-maintained map)
# ---------------------------------------------------------------------------


def domain_intents(kit: ChannelProductionKitV1, domain: str) -> tuple[str, ...]:
    """Semantic intents whose recipes taste-cover ``domain``."""

    intents = {
        recipe.semantic_intent
        for recipe in kit.recipes
        if domain in recipe.taste_domains
    }
    return tuple(sorted(intents))


def kit_domains(kit: ChannelProductionKitV1) -> tuple[str, ...]:
    """Every taste domain the kit covers (the valid ``domains`` vocabulary)."""

    return tuple(
        sorted({str(taste) for recipe in kit.recipes for taste in recipe.taste_domains})
    )


# ---------------------------------------------------------------------------
# Snippet resolution (index path + explicit fixture path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cue:
    """One subtitle cue in source-frame coordinates (pre record-shift)."""

    start_frame: int
    end_frame: int
    text: str


@dataclass(frozen=True, slots=True)
class _Snippet:
    """A resolved real-footage region plus its transcript cues."""

    origin: Literal["best_moment", "shot", "explicit"]
    media: Path
    rate: RationalFrameRate
    start_frame: int
    end_frame: int
    cues: tuple[_Cue, ...]


def _probe_rate_and_frames(tools: PinnedTools, media: Path) -> tuple[RationalFrameRate, int]:
    """Media frame rate + total frames from the pinned ffprobe (never lied to)."""

    report = probe_file(tools, media)
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    if video is None or video.r_frame_rate is None:
        raise KitPreviewError("media-missing", f"{media} has no readable video stream")
    num_text, _, den_text = video.r_frame_rate.partition("/")
    rate = RationalFrameRate(num=int(num_text), den=int(den_text or "1"))
    if video.nb_frames:
        total = int(video.nb_frames)
    elif video.duration is not None:
        total = round(Fraction(video.duration) * rate.as_fraction)
    else:
        raise KitPreviewError("media-missing", f"{media} reports neither frames nor duration")
    if total <= 0:
        raise KitPreviewError("media-missing", f"{media} has no usable frames ({total})")
    return rate, total


def _clip_window(
    start: int, end: int, anchor: int, rate: RationalFrameRate, total: int
) -> tuple[int, int]:
    """Clamp a shot window into the 10-20 s snippet band around the anchor."""

    seconds = Fraction(end - start) / rate.as_fraction
    if seconds > MAX_SNIPPET_SECONDS:
        half = int(MAX_SNIPPET_SECONDS * rate.as_fraction / 2)
        start, end = max(0, anchor - half), min(total, anchor + half)
    elif seconds < MIN_SNIPPET_SECONDS:
        grow = int(MIN_SNIPPET_SECONDS * rate.as_fraction) - (end - start)
        start, end = max(0, start - grow // 2), min(total, end + grow // 2)
    if end <= start:
        start, end = 0, total
    return start, end


def _first_source_id(connection: object) -> str | None:
    """Single-source episodes: the alphabetically-first source id (T8 bridge)."""

    row = connection.execute(  # type: ignore[attr-defined] (duckdb connection, structural)
        "SELECT source_id FROM mi_sources ORDER BY source_id LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def _resolve_snippet_from_index(episode_root: Path, tools: PinnedTools) -> _Snippet:
    """Representative window via MediaQueryApiV2 best_moments -> shots."""

    index_path = episode_root / INDEX_NAME
    mezzanine = episode_root.joinpath(*MEZZANINE_RELATIVE)
    if not mezzanine.is_file():
        raise KitPreviewError("media-missing", f"episode edit source missing: {mezzanine}")
    if not index_path.is_file():
        raise KitPreviewError(
            "snippet-source-missing",
            f"no media-intelligence index at {index_path} and no explicit "
            "snippet specs; kit previews need one of the two",
        )
    rate, total = _probe_rate_and_frames(tools, mezzanine)
    from services.media_query import (  # noqa: PLC0415 (keeps duckdb out of the import graph)
        v2_models as vm,
    )
    from services.media_query.index_v2 import open_read_only  # noqa: PLC0415
    from services.media_query.query_v2 import MediaQueryApiV2  # noqa: PLC0415

    connection = open_read_only(index_path)
    try:
        api = MediaQueryApiV2(connection)
        whole = vm.FrameSpan(start_frame=0, end_frame=total)
        page = vm.V2Pagination(limit=5, offset=0)
        best = api.best_moments(
            vm.BestMomentsRequest(
                span=whole, select_potentials=("high",), pagination=page
            )
        )
        if best.rows:
            row = best.rows[0]
            origin: str = "best_moment"
            shot_start, shot_end = row.span.start_frame, row.span.end_frame
            anchor = row.frame
        else:
            shots = api.shots(vm.ShotsRequest(span=whole, pagination=page))
            if not shots.rows:
                raise KitPreviewError(
                    "snippet-source-missing",
                    f"index {index_path} carries neither best moments nor shots",
                )
            first = shots.rows[0]
            origin = "shot"
            shot_start, shot_end = first.span.start_frame, first.span.end_frame
            anchor = (shot_start + shot_end) // 2
        start, end = _clip_window(shot_start, shot_end, anchor, rate, total)
        cues: tuple[_Cue, ...] = ()
        source_id = _first_source_id(connection)
        if source_id is not None:
            segments = api.transcript_range(
                vm.TranscriptRangeRequest(
                    source_id=source_id,
                    span=whole,
                    pagination=vm.V2Pagination(limit=50, offset=0),
                )
            )
            cues = tuple(
                _Cue(seg.span.start_frame, seg.span.end_frame, seg.text)
                for seg in segments.rows
                if seg.span.end_frame > start
                and seg.span.start_frame < end
                and seg.text.strip()
            )
    finally:
        connection.close()
    return _Snippet(
        origin=origin, media=mezzanine, rate=rate,
        start_frame=start, end_frame=end, cues=cues,
    )


def _snippet_from_specs(specs: Sequence[SnippetSpecV1], tools: PinnedTools) -> _Snippet:
    """The FIRST explicit spec is the previewed region (fixture episodes)."""

    spec = specs[0]
    media = Path(spec.media_path)
    if not media.is_file():
        raise KitPreviewError("media-missing", f"snippet media missing: {media}")
    rate, total = _probe_rate_and_frames(tools, media)
    if spec.end_frame > total:
        raise KitPreviewError(
            "snippet-source-missing",
            f"spec span [{spec.start_frame},{spec.end_frame}) exceeds {media} frames ({total})",
        )
    cues = (
        (_Cue(spec.start_frame, spec.end_frame, spec.subtitle_text),)
        if spec.subtitle_text
        else ()
    )
    return _Snippet(
        origin="explicit", media=media, rate=rate,
        start_frame=spec.start_frame, end_frame=spec.end_frame, cues=cues,
    )


# ---------------------------------------------------------------------------
# Minimal snippet Timeline IR + bindings (preview renderer reuse)
# ---------------------------------------------------------------------------


def _ms_snap_step(rate: RationalFrameRate) -> int:
    """Frame step whose boundaries are exact milliseconds (SRT cue requirement).

    Frame f is exact-ms at rate num/den iff num divides f*1000*den, i.e.
    iff f is a multiple of num / gcd(num, 1000*den): 30 fps -> 3, 15 fps -> 3.
    """

    return rate.num // gcd(rate.num, 1000 * rate.den)


def _wrap_cue_text(text: str, line_length: int | None) -> str:
    """Wrap cue text at ``line_length`` chars (the APPLIED subtitle param).

    ``None`` (recipe declares no line_length) leaves the text unwrapped —
    params a recipe does not declare are never invented.
    """

    if line_length is None or line_length <= 0 or len(text) <= line_length:
        return text
    return "\n".join(text[i : i + line_length] for i in range(0, len(text), line_length))


def _ir_digest(rate: RationalFrameRate, tracks: tuple[TimelineTrack0C, ...]) -> str:
    payload = json.dumps(
        {
            "rate": {"num": rate.num, "den": rate.den},
            "tracks": [track.model_dump(mode="json") for track in tracks],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _snippet_ir(
    snippet: _Snippet, *, with_subtitles: bool, line_length: int | None
) -> tuple[TimelineIr0C, tuple[TimelineItem0C, ...]]:
    """Minimal single-region IR: linked A/V items + optional wrapped cues."""

    rate = snippet.rate
    total = snippet.end_frame - snippet.start_frame
    source = SourceRef(
        source_id="kit-snippet",
        span=SourceFrameSpan(
            start_frame=snippet.start_frame, end_frame=snippet.end_frame, rate=rate
        ),
    )
    video = TimelineItem0C(
        item_id="kit-v-001",
        kind="video",
        source=source,
        record_span=RecordFrameSpan(start_frame=0, end_frame=total),
        av_link_id="kit-av-001",
    )
    audio = TimelineItem0C(
        item_id="kit-a-001",
        kind="audio",
        source=source,
        record_span=video.record_span,
        av_link_id="kit-av-001",
    )
    tracks: list[TimelineTrack0C] = [
        TimelineTrack0C(track=TrackRef0C(kind="video", index=1), items=(video,)),
        TimelineTrack0C(track=TrackRef0C(kind="audio", index=2), items=(audio,)),
    ]
    subtitle_items: tuple[TimelineItem0C, ...] = ()
    if with_subtitles and snippet.cues:
        step = _ms_snap_step(rate)
        items: list[TimelineItem0C] = []
        for index, cue in enumerate(snippet.cues):
            rel_start = max(0, cue.start_frame - snippet.start_frame)
            rel_end = min(total, cue.end_frame - snippet.start_frame)
            snapped_start = min(ceil(rel_start / step) * step, total)
            snapped_end = floor(rel_end / step) * step
            if snapped_end <= snapped_start:
                continue
            items.append(
                TimelineItem0C(
                    item_id=f"kit-s-{index:03d}",
                    kind="subtitle",
                    source=SourceRef(
                        source_id="kit-subtitle-table",
                        span=SourceFrameSpan(
                            start_frame=snippet.start_frame + snapped_start,
                            end_frame=snippet.start_frame + snapped_end,
                            rate=rate,
                        ),
                    ),
                    record_span=RecordFrameSpan(
                        start_frame=snapped_start, end_frame=snapped_end
                    ),
                    subtitle_text=_wrap_cue_text(cue.text, line_length),
                )
            )
        subtitle_items = tuple(items)
        if subtitle_items:
            tracks.append(
                TimelineTrack0C(
                    track=TrackRef0C(kind="subtitle", index=3), items=subtitle_items
                )
            )
    ir = TimelineIr0C(
        artifact_id="timeline-ir-kit-snippet",
        artifact_type="timeline_ir_0c",
        schema_version="timeline-ir-0c-v1",
        content_hash=_ir_digest(rate, tuple(tracks)),
        producer=PREVIEW_PRODUCER,
        inputs=(),
        rate=rate,
        tracks=tuple(tracks),
    )
    return ir, subtitle_items


def _snippet_bindings(
    snippet: _Snippet,
    subtitle_items: tuple[TimelineItem0C, ...],
    work_dir: Path,
) -> PreviewMediaBindings:
    """Bind A/V to the snippet medium; subtitle items to one generated SRT."""

    media = MediaBinding(media_path=str(snippet.media), sha256=sha256_file(snippet.media))
    bindings: list[ItemBinding] = [
        ItemBinding(item_id="kit-v-001", binding=media),
        ItemBinding(item_id="kit-a-001", binding=media),
    ]
    if subtitle_items:
        cues = expected_subtitle_cues(subtitle_items, snippet.rate)
        srt = work_dir / SRT_WORK_NAME
        atomic_write(srt, render_srt(cues))
        table = MediaBinding(media_path=str(srt), sha256=sha256_file(srt))
        bindings.extend(
            ItemBinding(item_id=item.item_id, binding=table) for item in subtitle_items
        )
    return PreviewMediaBindings(items=tuple(bindings), bgm=None)


def _snippet_info(snippet: _Snippet) -> KitSnippetInfoV1:
    return KitSnippetInfoV1(
        origin=snippet.origin,
        media_path=str(snippet.media),
        media_sha256=sha256_file(snippet.media),
        rate=snippet.rate,
        start_frame=snippet.start_frame,
        end_frame=snippet.end_frame,
        duration_seconds=float(
            Fraction(snippet.end_frame - snippet.start_frame) / snippet.rate.as_fraction
        ),
    )


def _burn_font_size(resolved_params: dict[str, float]) -> int:
    """Derive burned preview font size from the recipe's resolved params.

    ``subtitle/default`` declares ``font_size`` directly; ``subtitle/emphasis``
    declares ``emphasis_scale`` (and ``hold_frames``) so the burned size
    enlarges a fixed 24 px base. The 24 px base mirrors the default recipe's
    ``font_size.default`` (``channel-kit-v1.json``) and keeps emphasis visibly
    larger than default at the same 640x360 preview scale.
    """

    if "font_size" in resolved_params:
        return max(12, min(48, round(resolved_params["font_size"])))  # type: ignore[arg-type]
    scale = float(resolved_params.get("emphasis_scale", 1.2))
    return max(12, min(48, round(24 * scale)))  # type: ignore[arg-type]


def _subtitle_overlay_text(subtitle_items: tuple[TimelineItem0C, ...]) -> str:
    """Concatenate wrapped cue texts for the burn overlay (one block, bottom)."""

    if not subtitle_items:
        return ""
    # lines already wrapped at line_length; join distinct cues with newline
    return "\n".join(
        (item.subtitle_text or "") for item in subtitle_items if (item.subtitle_text or "").strip()
    )


def _timed_overlay_filter_complex(
    windows: Sequence[tuple[float, float]],
) -> str:
    """Filter graph that shows each overlay PNG only inside its cue window.

    Uses ``gte(t,start)*lt(t,end)`` so the interval is ``[start, end)`` —
    two cues whose SRT times abut at e.g. 2.8 s never appear together for a
    frame (``between`` is inclusive on both ends and would co-display).
    """

    if not windows:
        raise ValueError("no windows for timed overlay filter")
    if len(windows) == 1:
        start, end = windows[0]
        return (
            f"[0:v][1:v]overlay=0:0:enable='gte(t,{start:.3f})*lt(t,{end:.3f})'"
            f":shortest=1:format=yuv420,format=yuv420p[v]"
        )
    parts: list[str] = []
    for index, (start, end) in enumerate(windows):
        enable = f"gte(t,{start:.3f})*lt(t,{end:.3f})"
        if index == 0:
            parts.append(
                f"[0:v][1:v]overlay=0:0:enable='{enable}':format=yuv420[tmp{index}]"
            )
        elif index < len(windows) - 1:
            parts.append(
                f"[tmp{index - 1}][{index + 1}:v]overlay=0:0:enable='{enable}'"
                f":format=yuv420[tmp{index}]"
            )
        else:
            tail = ":shortest=1:format=yuv420,format=yuv420p[v]"
            parts.append(
                f"[tmp{index - 1}][{index + 1}:v]overlay=0:0:enable='{enable}'{tail}"
            )
    return ";".join(parts)


def _render_subtitle_overlay(text: str, font_size: int, out_path: Path) -> None:
    """Render ``text`` to a 640x360 transparent PNG anchored at the bottom.

    Uses Pillow with a CJK-capable TTF; falls back to the default bitmap font.
    White fill + black stroke keeps text readable over any footage and makes
    the two subtitle styles visually distinct even at low resolution.
    """

    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415  # runtime-only

    width, height = 640, 360
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Try CJK-capable fonts first so Japanese cues render instead of tofu.
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    )
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None
    last_error: Exception | None = None
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            font = ImageFont.truetype(str(path), font_size)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if font is None:
        # Fallback to Pillow's built-in bitmap font; keeps the pipeline alive
        # even in envs with no TTF (subtitle will be tiny but still burned).
        font = ImageFont.load_default()
        _ = last_error  # silence unused warning

    lines = text.split("\n") if "\n" in text else [text]
    # measure line metrics for bottom anchoring
    line_metrics = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
    line_heights = [metrics[3] - metrics[1] for metrics in line_metrics]
    line_widths = [metrics[2] - metrics[0] for metrics in line_metrics]
    spacing = max(2, font_size // 6)
    total_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
    # bottom margin ~18 px plus a semi-transparent plate
    margin_bottom = 18
    plate_padding = 8
    # draw plate behind text for contrast ( Pillow alpha compose )
    if total_height > 0:
        max_width = max(line_widths) if line_widths else 0
        plate_left = max(0, (width - max_width) // 2 - plate_padding)
        plate_right = min(width, (width + max_width) // 2 + plate_padding)
        plate_top = height - margin_bottom - total_height - plate_padding
        plate_bottom = height - margin_bottom + plate_padding
        draw.rounded_rectangle(
            (plate_left, plate_top, plate_right, plate_bottom),
            radius=6,
            fill=(0, 0, 0, 140),
        )
    cursor_y = height - margin_bottom - total_height
    for line, w, h in zip(lines, line_widths, line_heights, strict=True):
        cursor_x = (width - w) // 2
        draw.text(
            (cursor_x, cursor_y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255),
        )
        cursor_y += h + spacing
    image.save(out_path, format="PNG")


def _burn_ffmpeg_binary(tools: PinnedTools) -> Path:
    """Pick ffmpeg for subtitle burn: pinned lacks PNG, so use system if present.

    Initial render/probe stay pinned; only the Pillow PNG -> burned mp4
    overlay uses system ffmpeg when pinned cannot decode PNG (current pinned
    toolchain ships only mov/matroska demuxers). Falls back to pinned and
    surfaces its error if no system binary exists.
    """

    # Homebrew/aarch64 location used in this repo's CI/dev hosts
    for candidate in (Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return candidate
    return tools.ffmpeg


def _burn_subtitle_into_preview(
    tools: PinnedTools,
    source: Path,
    overlay_png: Path,
    dest: Path,
) -> None:
    """One extra ffmpeg pass: overlay the rendered subtitle PNG (legacy single-plate path).

    Retained for compatibility / single-cue fast path. Prefer
    :func:`_burn_timed_overlays` for per-cue timed burns.
    """

    _burn_timed_overlays(
        tools, source, ((overlay_png, 0.0, 9999.0),), dest,
    )


def _burn_timed_overlays(
    tools: PinnedTools,
    source: Path,
    overlays: Sequence[tuple[Path, float, float]],
    dest: Path,
) -> None:
    """One extra ffmpeg pass: overlay each cue PNG only inside its enable window.

    Each ``overlays`` entry is ``(png_path, start_sec, end_sec)`` in
    snippet-local seconds, derived from the cue's record span
    (``[start, end)``). Multiple looped PNG inputs are chained via
    :func:`_timed_overlay_filter_complex`.
    """

    from services.preview.tools import run_bounded  # noqa: PLC0415

    if not overlays:
        raise ValueError("no overlays to burn")
    tools.verify_current()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = _burn_ffmpeg_binary(tools)
    windows = tuple((float(start), float(end)) for _, start, end in overlays)
    filter_complex = _timed_overlay_filter_complex(windows)
    input_parts: list[str] = [str(ffmpeg_bin), "-nostdin", "-y", "-v", "error", "-i", str(source)]
    for png, _, _ in overlays:
        input_parts.extend(["-loop", "1", "-i", str(png)])
    common_tail: tuple[str, ...]
    if ffmpeg_bin == tools.ffmpeg:
        common_tail = (
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a",
            "-c:v", "h264_videotoolbox",
            "-b:v", "1500k",
            "-pix_fmt", "yuv420p",
            "-video_track_timescale", "30000",
            "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-ac", "1",
            "-movflags", "+faststart",
            "-shortest", str(dest),
        )
    else:
        common_tail = (
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-ac", "1",
            "-movflags", "+faststart",
            "-shortest", str(dest),
        )
    command = tuple(input_parts) + common_tail
    result = run_bounded(list(command))
    if result.returncode != 0:
        raise KitPreviewError(
            "render-failed",
            f"subtitle burn overlay failed: {result.stderr.strip()[-1500:]}",
        )


def _render_candidate(
    *,
    previews_root: Path,
    domain: str,
    selection: RecipeSelection,
    snippet: _Snippet,
    tools: PinnedTools,
) -> KitPreviewCandidateV1:
    """Render one candidate through the real preview renderer; return its entry."""

    recipe = selection.recipe
    sanitized = recipe.recipe_id.replace("/", "__")
    domain_dir = previews_root / domain
    work_dir = domain_dir / f".work-{sanitized}"
    work_dir.mkdir(parents=True, exist_ok=True)
    with_subtitles = domain == "subtitle"
    line_length = (
        int(selection.resolved_params["line_length"])
        if with_subtitles and "line_length" in selection.resolved_params
        else None
    )
    ir, subtitle_items = _snippet_ir(
        snippet, with_subtitles=with_subtitles, line_length=line_length
    )
    bindings = _snippet_bindings(snippet, subtitle_items, work_dir)
    target: Path | None = None
    try:
        render_preview(None, ir, bindings, work_dir, tools=tools)
        soft_rendered = work_dir / PREVIEW_NAME
        target = domain_dir / f"{sanitized}.mp4"
        if with_subtitles and subtitle_items:
            overlays: list[tuple[Path, float, float]] = []
            font_size = _burn_font_size(dict(selection.resolved_params))
            for idx, item in enumerate(subtitle_items):
                text = (item.subtitle_text or "").strip()
                if not text:
                    continue
                num, den = snippet.rate.num, snippet.rate.den
                start_ms = (item.record_span.start_frame * den * 1000 + num // 2) // num
                end_ms = (item.record_span.end_frame * den * 1000 + num // 2) // num
                if end_ms <= start_ms:
                    continue
                overlay = work_dir / f"subtitle-overlay-{idx:03d}.png"
                _render_subtitle_overlay(text, font_size, overlay)
                overlays.append((overlay, start_ms / 1000.0, end_ms / 1000.0))
            if overlays:
                burned = work_dir / "burned.mp4"
                _burn_timed_overlays(tools, soft_rendered, tuple(overlays), burned)
                burned.replace(target)
            else:
                soft_rendered.replace(target)
        else:
            soft_rendered.replace(target)
    except KitPreviewError:
        raise
    except Exception as error:
        raise KitPreviewError(
            "render-failed",
            f"recipe {recipe.recipe_id} snippet render failed: {error}",
        ) from error
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    if target is None:  # pragma: no cover
        raise KitPreviewError("render-failed", "burn produced no target")
    return KitPreviewCandidateV1(
        recipe_id=recipe.recipe_id,
        semantic_intent=recipe.semantic_intent,
        selection_rationale=selection.selection_rationale,
        resolved_params=dict(selection.resolved_params),
        parameter_bounds={
            param: ParameterBoundV1(
                min=bounds.min, max=bounds.max, default=bounds.default, unit=bounds.unit
            )
            for param, bounds in recipe.parameter_bounds.items()
        },
        file=f"{domain}/{sanitized}.mp4",
        sha256=sha256_file(target),
    )


# ---------------------------------------------------------------------------
# plan_previews
# ---------------------------------------------------------------------------


def plan_previews(  # noqa: PLR0913 (plan-mandated signature + injected tools/evidence)
    kit: ChannelProductionKitV1,
    episode_root: Path,
    domains: Sequence[str] = DEFAULT_DOMAINS,
    snippet_specs: Sequence[SnippetSpecV1] | None = None,
    *,
    tools: PinnedTools | None = None,
    taste_evidence: Sequence[Any] | None = None,
) -> KitPreviewManifestV1:
    """Render per-domain candidate snippets; write kit-previews/manifest.json.

    Typed refusals (``KitPreviewError``): unknown domain, missing committed
    selection (``selection-missing`` — PLAN_COMMITTED must precede kit
    previews), missing snippet source, or render failure.
    """

    requested = tuple(dict.fromkeys(domains))
    if not requested:
        raise KitPreviewError("unknown-domain", "no domains requested")
    known = kit_domains(kit)
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise KitPreviewError(
            "unknown-domain",
            f"unknown kit domains {unknown}; the kit covers {list(known)}",
        )
    selection_store = episode_root.joinpath(*SELECTION_STORE_RELATIVE)
    if not selection_store.is_file():
        raise KitPreviewError(
            "selection-missing",
            f"no committed selection at {selection_store}; commit the selection "
            "plan (PLAN_COMMITTED) before planning kit previews",
        )
    resolved_tools = tools if tools is not None else load_pinned_tools()
    if snippet_specs:
        snippet = _snippet_from_specs(tuple(snippet_specs), resolved_tools)
    else:
        snippet = _resolve_snippet_from_index(episode_root, resolved_tools)

    previews_root = episode_root / KIT_PREVIEWS_DIR
    shutil.rmtree(previews_root, ignore_errors=True)
    previews_root.mkdir(parents=True, exist_ok=True)

    domain_entries: list[KitPreviewDomainV1] = []
    for domain in requested:
        intents = domain_intents(kit, domain)
        if not intents:
            raise KitPreviewError("no-candidates", f"kit carries no recipe for domain {domain!r}")
        candidates = tuple(
            _render_candidate(
                previews_root=previews_root,
                domain=domain,
                selection=select_recipe(kit, intent, taste_evidence=taste_evidence),
                snippet=snippet,
                tools=resolved_tools,
            )
            for intent in intents
        )
        domain_entries.append(
            KitPreviewDomainV1(
                domain=domain,
                intents=intents,
                snippet=_snippet_info(snippet),
                candidates=candidates,
            )
        )
    manifest = KitPreviewManifestV1(episode_id=episode_root.name, domains=tuple(domain_entries))
    atomic_write(previews_root / MANIFEST_NAME, canonical_model_bytes(manifest))
    return manifest


# ---------------------------------------------------------------------------
# Runtime selection record (kit-selections.json)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_selection_record(path: Path) -> KitSelectionRecordV1:
    """Strictly revalidate the record on every read (stale-state guard)."""

    try:
        return KitSelectionRecordV1.model_validate_json(path.read_bytes())
    except OSError as error:
        raise KitPreviewError("record-unreadable", str(error)) from error
    except ValueError as error:
        raise KitPreviewError(
            "record-invalid", f"{path} is not a kit-selection-v1 record: {error}"
        ) from error


def append_selection_entry(
    path: Path, episode_id: str, entry: KitDomainSelectionV1
) -> KitSelectionRecordV1:
    """Append one choice (latest per domain wins) and atomically rewrite."""

    if path.is_file():
        record = load_selection_record(path)
        if record.episode_id != episode_id:
            raise KitPreviewError(
                "record-invalid",
                f"{path} belongs to episode {record.episode_id}, not {episode_id}",
            )
        updated = KitSelectionRecordV1(episode_id=episode_id, entries=(*record.entries, entry))
    else:
        updated = KitSelectionRecordV1(episode_id=episode_id, entries=(entry,))
    atomic_write(path, canonical_model_bytes(updated))
    return updated


def latest_selections(record: KitSelectionRecordV1) -> dict[str, KitDomainSelectionV1]:
    """Latest entry per domain (append-only log; last write wins)."""

    latest: dict[str, KitDomainSelectionV1] = {}
    for entry in record.entries:
        latest[entry.domain] = entry
    return latest


def recipe_selection_from_record(
    kit: ChannelProductionKitV1, record: KitSelectionRecordV1, domain: str
) -> RecipeSelection | None:
    """Round-trip a recorded choice back to the RecipeSelection the build resolves.

    ``None`` means the operator chose どちらも不要/現状維持 for the domain.
    The recorded recipe must be the SAME recipe ``select_recipe`` resolves
    for its intent (exact-intent match, task-37 tie-break); anything else
    is a typed refusal, never a silent substitution.
    """

    entry = latest_selections(record).get(domain)
    if entry is None or entry.recipe_id is None:
        return None
    recipe = next((r for r in kit.recipes if r.recipe_id == entry.recipe_id), None)
    if recipe is None:
        raise KitPreviewError(
            "unknown-recipe",
            f"recorded recipe {entry.recipe_id!r} for domain {domain!r} is not in the kit",
        )
    resolved = select_recipe(kit, recipe.semantic_intent)
    if resolved.recipe.recipe_id != entry.recipe_id:
        raise KitPreviewError(
            "selection-ambiguous",
            f"intent {recipe.semantic_intent!r} now resolves to "
            f"{resolved.recipe.recipe_id!r}, not the recorded {entry.recipe_id!r}",
        )
    return resolved


__all__ = [
    "DEFAULT_DOMAINS",
    "INDEX_NAME",
    "KIT_PREVIEWS_DIR",
    "MANIFEST_NAME",
    "MEZZANINE_RELATIVE",
    "SELECTIONS_NAME",
    "SELECTION_STORE_RELATIVE",
    "KitDomainSelectionV1",
    "KitPreviewCandidateV1",
    "KitPreviewDomainV1",
    "KitPreviewError",
    "KitPreviewManifestV1",
    "KitSelectionRecordV1",
    "KitSnippetInfoV1",
    "SnippetSpecV1",
    "append_selection_entry",
    "domain_intents",
    "kit_domains",
    "latest_selections",
    "load_selection_record",
    "plan_previews",
    "recipe_selection_from_record",
]
