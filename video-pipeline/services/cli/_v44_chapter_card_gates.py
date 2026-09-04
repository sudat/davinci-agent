"""Typed refusal gates, protection set, and audio identity proof."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_chapter_card_media import decode_pcm_s16le
from services.cli._v44_chapter_card_plan import (
    BASE_PLAN_SHA256,
    CANVAS_H,
    CANVAS_W,
    EPISODE_ID,
    FPS,
    OUTPUT_FRAMES,
    OUTPUT_PCM_BYTES,
    OUTPUT_SAMPLES,
    PREFIX_BYTES,
    SAMPLE_RATE,
    SILENCE_BYTES,
    SOURCE_PCM_SHA256,
    STEREO_CHANNELS,
    VIDEO_TIMESCALE,
    subtitle_item_count,
)
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.cli._v44_chapter_card_probe import MasterFacts
    from services.preview.tools import PinnedTools

EXPECTED_SUBTITLE_ITEMS: Final = 97
EXPECTED_VIDEO_CODEC: Final = "h264"
EXPECTED_VIDEO_TIME_BASE: Final = f"1/{VIDEO_TIMESCALE}"


class ChapterCardInsertError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def sha256_bytes(data: bytes | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def common_root(episode_root: Path, diag_finishing_root: Path) -> Path:
    try:
        return Path(os.path.commonpath([episode_root, diag_finishing_root]))
    except ValueError as error:
        raise ChapterCardInsertError(
            "protection-root-invalid",
            f"no common ancestor: {episode_root} vs {diag_finishing_root}",
        ) from error


def protected_paths(*, episode_root: Path, diag_finishing_root: Path) -> tuple[Path, ...]:
    """Every file the insertion run must leave byte-identical."""

    store = episode_root / "review" / "store"
    return (
        *(
            store / name
            for name in (
                "plan-v1.json",
                "plan-v2.json",
                "plan-v3.json",
                "ir-v1.json",
                "ir-v2.json",
                "ir-v3.json",
                "versions.json",
            )
        ),
        episode_root / "review" / "events.jsonl",
        episode_root / "review" / "events.jsonl.seal",
        *(
            episode_root / "review-store" / name
            for name in (
                "events.jsonl", "events.jsonl.seal", "plan-v1.json", "ir-v1.json",
                "versions.json",
            )
        ),
        episode_root / "runtime" / "chapter-title-proposal.json",
        diag_finishing_root / "theme-full-render" / "v44-real-01-theme-full.mp4",
        diag_finishing_root / "final-resolve-render" / f"finishing-native-{EPISODE_ID}.mp4",
        diag_finishing_root / "resolve-render" / f"{EPISODE_ID}-final-v5.mp4",
    )


def hash_protected(paths: tuple[Path, ...], *, root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            raise ChapterCardInsertError("protected-missing", f"protected file absent: {path}")
        try:
            digest = sha256_file(path)
        except OSError as error:
            raise ChapterCardInsertError(
                "protected-unreadable", f"cannot read protected file {path}: {error}"
            ) from error
        hashes[path.relative_to(root).as_posix()] = digest
    return hashes


def require_protection_unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    if after != before:
        drifted = sorted(key for key in before if before.get(key) != after.get(key))
        raise ChapterCardInsertError("protected-drift", f"protected files changed: {drifted}")


def require_source_pcm_canonical(source_pcm: bytes) -> None:
    if sha256_bytes(source_pcm) != SOURCE_PCM_SHA256:
        raise ChapterCardInsertError("source-pcm-mismatch", "canonical source PCM hash drifted")


def require_plan_v3(plan_v3: Path) -> int:
    subtitle_items = subtitle_item_count(plan_v3)
    try:
        plan_digest = sha256_file(plan_v3)
    except OSError as error:
        raise ChapterCardInsertError(
            "plan-unreadable", f"cannot read review plan {plan_v3}: {error}"
        ) from error
    if plan_digest != BASE_PLAN_SHA256 or subtitle_items != EXPECTED_SUBTITLE_ITEMS:
        raise ChapterCardInsertError(
            "plan-drift", f"plan-v3: {subtitle_items} subtitle items or hash drifted"
        )
    return subtitle_items


def require_master_facts(facts: MasterFacts, master: Path) -> None:
    if (
        facts.video_codec != EXPECTED_VIDEO_CODEC
        or facts.video_time_base != EXPECTED_VIDEO_TIME_BASE
        or facts.video_frames != OUTPUT_FRAMES
        or facts.avg_frame_rate != f"{FPS}/1"
        or (facts.width, facts.height) != (CANVAS_W, CANVAS_H)
        or facts.audio_codec != "pcm_s16le"
        or facts.sample_rate != SAMPLE_RATE
        or facts.channels != STEREO_CHANNELS
        or facts.audio_samples != OUTPUT_SAMPLES
    ):
        raise ChapterCardInsertError(
            "master-facts-mismatch", f"probed master facts drifted for {master}: {facts}"
        )


def _matches_splice(
    candidate: bytes | memoryview, source: memoryview, *, total: int
) -> bool:
    """True when candidate is exactly source prefix + zeros + source suffix."""

    view = candidate if isinstance(candidate, memoryview) else memoryview(candidate)
    return (
        len(view) == total
        and view[:PREFIX_BYTES] == source[:PREFIX_BYTES]
        and not any(view[PREFIX_BYTES : PREFIX_BYTES + SILENCE_BYTES])
        and view[PREFIX_BYTES + SILENCE_BYTES :] == source[PREFIX_BYTES:]
    )


def audio_proof(
    tools: PinnedTools, master: Path, source_pcm: bytes, constructed: bytes
) -> dict[str, object]:
    """Decode the master once and prove the exact prefix/silence/suffix splice.

    All part comparisons and hashes run over memoryview slices, so no second
    50 MB copy is ever materialized.
    """

    decoded = memoryview(decode_pcm_s16le(tools, master))
    source = memoryview(source_pcm)
    prefix = decoded[:PREFIX_BYTES]
    silence = decoded[PREFIX_BYTES : PREFIX_BYTES + SILENCE_BYTES]
    suffix = decoded[PREFIX_BYTES + SILENCE_BYTES :]
    if not _matches_splice(decoded, source, total=OUTPUT_PCM_BYTES):
        raise ChapterCardInsertError(
            "audio-identity-failed",
            f"decoded master PCM is not prefix+silence+source-suffix "
            f"({len(decoded)} bytes, expected {OUTPUT_PCM_BYTES})",
        )
    if not _matches_splice(constructed, source, total=OUTPUT_PCM_BYTES):
        raise ChapterCardInsertError(
            "audio-identity-failed",
            "constructed output PCM is not the canonical splice of the source",
        )
    reconstructed = hashlib.sha256(prefix)
    reconstructed.update(suffix)
    return {
        "prefix_bytes": PREFIX_BYTES,
        "prefix_sha256": sha256_bytes(prefix),
        "silence_bytes": SILENCE_BYTES,
        "silence_all_zero": not any(silence),
        "silence_sha256": sha256_bytes(silence),
        "suffix_bytes": len(suffix),
        "suffix_sha256": sha256_bytes(suffix),
        "total_sample_frames": len(decoded) // 4,
        "reconstructed_matches_source": True,
        "reconstructed_sha256": reconstructed.hexdigest(),
        "source_pcm_sha256": sha256_bytes(source),
        "splice": f"source[0:{PREFIX_BYTES}] + {SILENCE_BYTES} zero bytes + "
        f"source[{PREFIX_BYTES}:] (build_output_pcm of {len(source_pcm)} canonical bytes)",
        "build_output_pcm_used": True,
    }


__all__ = [
    "EXPECTED_SUBTITLE_ITEMS",
    "EXPECTED_VIDEO_CODEC",
    "EXPECTED_VIDEO_TIME_BASE",
    "ChapterCardInsertError",
    "audio_proof",
    "common_root",
    "hash_protected",
    "protected_paths",
    "require_master_facts",
    "require_plan_v3",
    "require_protection_unchanged",
    "require_source_pcm_canonical",
    "sha256_bytes",
]
