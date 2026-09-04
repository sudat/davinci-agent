"""Rebinding the frozen insertion contract to a small synthetic episode."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import services.cli._v44_chapter_card_evidence as evidence
import services.cli._v44_chapter_card_gates as gates
import services.cli._v44_chapter_card_pair_qa as pair_qa
import services.cli._v44_chapter_card_plan as plan
import services.cli._v44_chapter_card_preflight as preflight
import services.cli._v44_chapter_card_qa as qa
import services.cli._v44_chapter_card_run as run
import services.presentation.chapter_card as card

if TYPE_CHECKING:
    import pytest


@dataclass(frozen=True, slots=True)
class ContractIdentity:
    """Measured hashes binding the scaled world to its synthetic media."""

    source_video_sha256: str
    source_pcm_sha256: str
    plan_sha256: str


def install_scaled_contract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canvas: tuple[int, int],
    identity: ContractIdentity,
    subtitle_items: int,
) -> None:
    """Rebind every frozen insertion constant to a 60/20/45-frame synthetic world."""

    samples_per_frame = 1600
    source_frames, record_frame, card_frames = 60, 20, 45
    output_frames = source_frames + card_frames
    insert_sample = record_frame * samples_per_frame
    silence_samples = card_frames * samples_per_frame
    source_samples = source_frames * samples_per_frame
    output_samples = source_samples + silence_samples
    prefix_bytes = insert_sample * 4
    silence_bytes = silence_samples * 4
    width, height = canvas
    bindings: dict[tuple[ModuleType, str], object] = {
        (plan, "SOURCE_FRAMES"): source_frames,
        (plan, "RECORD_FRAME"): record_frame,
        (plan, "CARD_FRAMES"): card_frames,
        (plan, "OUTPUT_FRAMES"): output_frames,
        (plan, "INSERT_SAMPLE"): insert_sample,
        (plan, "SILENCE_SAMPLES"): silence_samples,
        (plan, "SOURCE_SAMPLES"): source_samples,
        (plan, "OUTPUT_SAMPLES"): output_samples,
        (plan, "PREFIX_BYTES"): prefix_bytes,
        (plan, "SILENCE_BYTES"): silence_bytes,
        (plan, "SOURCE_PCM_BYTES"): source_samples * 4,
        (plan, "OUTPUT_PCM_BYTES"): output_samples * 4,
        (plan, "SOURCE_VIDEO_SHA256"): identity.source_video_sha256,
        (plan, "SOURCE_PCM_SHA256"): identity.source_pcm_sha256,
        (preflight, "SOURCE_VIDEO_SHA256"): identity.source_video_sha256,
        (gates, "SOURCE_PCM_SHA256"): identity.source_pcm_sha256,
        (gates, "CANVAS_W"): width,
        (gates, "CANVAS_H"): height,
        (gates, "BASE_PLAN_SHA256"): identity.plan_sha256,
        (gates, "EXPECTED_SUBTITLE_ITEMS"): subtitle_items,
        (gates, "OUTPUT_FRAMES"): output_frames,
        (gates, "OUTPUT_SAMPLES"): output_samples,
        (gates, "PREFIX_BYTES"): prefix_bytes,
        (gates, "SILENCE_BYTES"): silence_bytes,
        (gates, "OUTPUT_PCM_BYTES"): output_samples * 4,
        (evidence, "BASE_PLAN_SHA256"): identity.plan_sha256,
        (evidence, "CARD_FRAMES"): card_frames,
        (evidence, "RECORD_FRAME"): record_frame,
        (evidence, "SOURCE_FRAMES"): source_frames,
        (evidence, "SOURCE_VIDEO_SHA256"): identity.source_video_sha256,
        (evidence, "VIDEO_BITRATE"): "24M",
        (run, "CARD_FRAMES"): card_frames,
        (run, "RECORD_FRAME"): record_frame,
        (run, "VIDEO_BITRATE"): "24M",
        (run, "CANVAS_W"): width,
        (run, "CANVAS_H"): height,
        (qa, "CANVAS_W"): width,
        (qa, "CANVAS_H"): height,
        (qa, "FRAME_BYTES"): width * height * 3,
        (qa, "CARD_PNG_SAMPLES"): (record_frame, record_frame + 20, record_frame + 44),
        (pair_qa, "SAMPLED_PAIRS"): (
            (0, 0),
            (record_frame - 1, record_frame - 1),
            (record_frame + card_frames, record_frame),
            (output_frames - 1, source_frames - 1),
        ),
        (pair_qa, "CANVAS_W"): width,
        (pair_qa, "CANVAS_H"): height,
        (card, "CANVAS_W"): width,
        (card, "CANVAS_H"): height,
        (card, "FONT_SIZE_PX"): 12,
        (card, "MAX_LINE_HEIGHT"): 40,
    }
    for (target, name), value in bindings.items():
        monkeypatch.setattr(target, name, value)


def synthetic_source(ffmpeg: Path, path: Path, *, width: int, height: int) -> None:
    """60-frame testsrc2 + 2s sine as h264/pcm_s16le, timescale 15360."""

    result = subprocess.run(
        (
            str(ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-frames:v",
            "60",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-video_track_timescale",
            "15360",
            str(path),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise AssertionError(f"fixture ffmpeg failed: {result.stderr[-400:]}")


__all__ = ["ContractIdentity", "install_scaled_contract", "synthetic_source"]
