"""Intro/outro slate and audio-preset element strategies with readback rows."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.fixed_presentation_models import (
    ElementStrategy,
    FixedPresentationMismatch,
    RungAttempt,
    SlateEvidence,
)

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_bridge.base_cut_models import TimelineSnapshot

SLATE_FRAMES: Final = 30
INTRO_RECORD: Final = 0
OUTRO_RECORD: Final = 630


def slate_rows(
    snapshot: TimelineSnapshot, media: dict[str, str], origin: int
) -> tuple[SlateEvidence, ...]:
    rows: list[SlateEvidence] = []
    for item_id, source_id, record in (
        ("intro-001", "intro", INTRO_RECORD),
        ("outro-001", "outro", OUTRO_RECORD),
    ):
        wanted = Path(media[source_id]).resolve()
        video = next(
            (
                item
                for item in snapshot.items
                if item.kind == "video" and item.record_start == record + origin
            ),
            None,
        )
        audio = next(
            (
                item
                for item in snapshot.items
                if item.kind == "audio" and item.record_start == record + origin
            ),
            None,
        )
        if video is None or audio is None:
            rows.append(_missing_slate(item_id, wanted, record, origin))
            continue
        rows.append(
            SlateEvidence(
                item_id=item_id,
                media_path=video.media_path,
                record_start=video.record_start,
                record_end=video.record_end,
                duration_frames=video.record_end - video.record_start,
                link_partner_id=audio.unique_id,
                link_ok=video.linked_ids == frozenset({audio.unique_id}),
                media_ok=Path(video.media_path).resolve() == wanted,
                span_ok=video.record_start == record + origin
                and video.record_end == record + SLATE_FRAMES + origin,
            )
        )
    return tuple(rows)


def _missing_slate(item_id: str, wanted: Path, record: int, origin: int) -> SlateEvidence:
    return SlateEvidence(
        item_id=item_id,
        media_path=str(wanted),
        record_start=record + origin,
        record_end=record + SLATE_FRAMES + origin,
        duration_frames=0,
        link_partner_id=None,
        link_ok=False,
        media_ok=False,
        span_ok=False,
    )


def slates_strategy(slates: tuple[SlateEvidence, ...]) -> ElementStrategy:
    ok = slates_all_ok(slates)
    return ElementStrategy(
        element="intro-outro",
        strategy="direct",
        status="verified" if ok else "partial",
        reason=(
            "media-backed AppendToTimeline placement verified by identity, duration, "
            "and A/V link readback"
        ),
        limitations=(
            "identity is the resolved media file path; media bytes are not checksummed at placement"
        ),
        ladder=(
            RungAttempt(rung="direct", available=True, reason="existing base-cut placement path"),
        ),
    )


def audio_strategy(
    manifest: Phase0AFixtureManifest, render_mismatches: tuple[FixedPresentationMismatch, ...]
) -> ElementStrategy:
    preset = manifest.recipe.render_preset
    ok = not any(m.code == "audio-preset-mismatch" for m in render_mismatches)
    return ElementStrategy(
        element="audio-preset",
        strategy="direct",
        status="verified" if ok else "partial",
        reason=(
            f"stereo timeline audio track plus SetRenderSettings AudioCodec={preset.audio_codec}/"
            f"AudioSampleRate={preset.audio_sample_rate}; verified on the rendered output"
        ),
        limitations="preset verified at the container level; no Fairlight per-track processing",
        ladder=(
            RungAttempt(
                rung="direct",
                available=True,
                reason="official AddTrack/SetRenderSettings APIs; ffprobe readback of the render",
            ),
        ),
    )


def slates_all_ok(slates: tuple[SlateEvidence, ...]) -> bool:
    return all(row.media_ok and row.span_ok and row.link_ok for row in slates)
