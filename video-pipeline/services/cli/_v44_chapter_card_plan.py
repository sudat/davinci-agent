"""Frozen expectations and pure splice math for the approved chapter-card insertion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from services.cli._v44_chapter_title_contracts import ChapterTitleProposalSidecar
from services.creative_plan.presentation_intents import ChapterCardParams

EPISODE_ID: Final = "ep-457dfac97989568e"
TITLE: Final = "どこにもないカメラバッグ"
CANDIDATE_ID: Final = "a08179562756c73040052a26091aa26da2118e5728dc3d22c8352048ce5e1914"
MODEL_ID: Final = "gpt-5.6-sol"
BASE_PLAN_SHA256: Final = "bb025567cc1d10bb768aa595deec99af503631d6bbd3affbf6224ea487d4ebc3"
BASE_IR_SHA256: Final = "298a68764493019a409931cbedcd70a15bc70460ddc93f90c02ff3ee93bf6a12"
SOURCE_VIDEO_SHA256: Final = "ddb3e1f5838e0b5bb057ec47e97195c09a93b2dfb5041373482e81fc0157656f"
SOURCE_PCM_SHA256: Final = "af4a5a032605077457ce481c8b6d067fe5d1cd92b53a9033a472469a160ecba7"
LEFT_ITEM_ID: Final = "s20"
RIGHT_ITEM_ID: Final = "s21"
BOUNDARY_SOURCE_FRAME: Final = 1845

FPS: Final = 30
SAMPLE_RATE: Final = 48000
STEREO_CHANNELS: Final = 2
SAMPLES_PER_FRAME: Final = SAMPLE_RATE // FPS  # 1600
SOURCE_FRAMES: Final = 7792
RECORD_FRAME: Final = 1632
CARD_FRAMES: Final = 45
OUTPUT_FRAMES: Final = SOURCE_FRAMES + CARD_FRAMES  # 7837
INSERT_SAMPLE: Final = RECORD_FRAME * SAMPLES_PER_FRAME  # 2_611_200
SILENCE_SAMPLES: Final = CARD_FRAMES * SAMPLES_PER_FRAME  # 72_000
SOURCE_SAMPLES: Final = 12_467_136
OUTPUT_SAMPLES: Final = SOURCE_SAMPLES + SILENCE_SAMPLES  # 12_539_136
BYTES_PER_SAMPLE_FRAME: Final = 4  # s16le stereo
PREFIX_BYTES: Final = INSERT_SAMPLE * BYTES_PER_SAMPLE_FRAME  # 10_444_800
SILENCE_BYTES: Final = SILENCE_SAMPLES * BYTES_PER_SAMPLE_FRAME  # 288_000
SOURCE_PCM_BYTES: Final = SOURCE_SAMPLES * BYTES_PER_SAMPLE_FRAME  # 49_868_544
OUTPUT_PCM_BYTES: Final = OUTPUT_SAMPLES * BYTES_PER_SAMPLE_FRAME  # 50_156_544

CANVAS_W: Final = 1920
CANVAS_H: Final = 1080
VIDEO_BITRATE: Final = "24M"
VIDEO_TIMESCALE: Final = 15360
# The exact approved font: macOS Hiragino Sans GB collection, bold W6 face.
FONT_SHA256: Final = "f887295caf2881cab9554b14c5ab4c9ee624c3895599da152ec37416b5aefae0"
FONT_FACE: Final = ("Hiragino Sans GB", "W6")

OPERATOR_TITLE_STATEMENTS: Final = (
    "オペレーターが候補「どこにもないカメラバッグ」を承認した。",
    "「バッグ」はAIによる言い換えであり、発話には現れない語である。",
    "テーマの文言は「ケース」を用いており、タイトルとは一致しない。",
    "オペレーターは上記2点を確認したうえで、タイトルを変更せずに選択した。",
)
OPERATOR_AUDIO_STATEMENT: Final = (
    "オペレーター承認のうえで1.5秒の無音を挿入。それ以外の音声改変なし"
)


class ChapterCardPlanError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ApprovedCard:
    title: str
    candidate_id: str
    model_id: str
    base_plan_sha256: str
    base_ir_sha256: str
    left_item_id: str
    right_item_id: str
    record_frame: int
    source_frame: int
    card_frames: int


def load_approved_card(path: Path) -> ApprovedCard:
    """Validate the runtime sidecar against the frozen operator-approved selection."""

    try:
        sidecar = ChapterTitleProposalSidecar.model_validate_json(path.read_bytes())
    except OSError as error:
        raise ChapterCardPlanError(
            "sidecar-unreadable", f"cannot read proposal: {error}"
        ) from error
    except ValidationError as error:
        raise ChapterCardPlanError(
            "sidecar-invalid", f"proposal validation failed: {error}"
        ) from error
    selected = next(
        (item for item in sidecar.candidates if item.candidate_id == CANDIDATE_ID), None
    )
    if selected is None:
        raise ChapterCardPlanError(
            "candidate-missing", f"approved candidate {CANDIDATE_ID} is absent from {path}"
        )
    params = selected.presentation_intent.params
    if not isinstance(params, ChapterCardParams):
        raise ChapterCardPlanError(
            "candidate-kind-drift",
            f"approved candidate params are {type(params).__name__}, expected a chapter card",
        )
    title = params.title
    duration = params.duration_frames
    span = selected.presentation_intent.target_span
    if (
        sidecar.episode_id != EPISODE_ID
        or sidecar.base_plan_sha256 != BASE_PLAN_SHA256
        or sidecar.base_ir_sha256 != BASE_IR_SHA256
        or sidecar.model_pin.model_id != MODEL_ID
        or sidecar.boundary.record_frame != RECORD_FRAME
        or sidecar.boundary.source_frame != BOUNDARY_SOURCE_FRAME
        or (sidecar.boundary.left_item_id, sidecar.boundary.right_item_id)
        != (LEFT_ITEM_ID, RIGHT_ITEM_ID)
        or sidecar.chapter_card_duration_frames != CARD_FRAMES
        or duration != CARD_FRAMES
        or (span.start_frame, span.end_frame) != (RECORD_FRAME, RECORD_FRAME + CARD_FRAMES)
        or title != TITLE
    ):
        raise ChapterCardPlanError(
            "sidecar-drift",
            f"proposal does not match the approved selection: title={title!r}, "
            f"duration={duration}, boundary={sidecar.boundary.record_frame}",
        )
    return ApprovedCard(
        title=title,
        candidate_id=CANDIDATE_ID,
        model_id=sidecar.model_pin.model_id,
        base_plan_sha256=sidecar.base_plan_sha256,
        base_ir_sha256=sidecar.base_ir_sha256,
        left_item_id=sidecar.boundary.left_item_id,
        right_item_id=sidecar.boundary.right_item_id,
        record_frame=sidecar.boundary.record_frame,
        source_frame=sidecar.boundary.source_frame,
        card_frames=duration,
    )


def build_output_pcm(source_pcm: bytes) -> bytes:
    """Master PCM bytes: source prefix + exact silence + complete source suffix."""

    if len(source_pcm) != SOURCE_PCM_BYTES:
        raise ChapterCardPlanError(
            "source-pcm-length",
            f"canonical source PCM is {len(source_pcm)} bytes, expected {SOURCE_PCM_BYTES}",
        )
    return source_pcm[:PREFIX_BYTES] + b"\x00" * SILENCE_BYTES + source_pcm[PREFIX_BYTES:]


def split_output_pcm(output_pcm: bytes) -> tuple[bytes, bytes, bytes]:
    """Split master PCM into the three proof parts (prefix, silence, suffix)."""

    if len(output_pcm) != OUTPUT_PCM_BYTES:
        raise ChapterCardPlanError(
            "output-pcm-length",
            f"master PCM is {len(output_pcm)} bytes, expected {OUTPUT_PCM_BYTES}",
        )
    return (
        output_pcm[:PREFIX_BYTES],
        output_pcm[PREFIX_BYTES : PREFIX_BYTES + SILENCE_BYTES],
        output_pcm[PREFIX_BYTES + SILENCE_BYTES :],
    )


def frame_kind(output_frame: int) -> Literal["source", "card"]:
    if RECORD_FRAME <= output_frame < RECORD_FRAME + CARD_FRAMES:
        return "card"
    return "source"


def source_frame_for_output(output_frame: int) -> int | None:
    """Source frame shown at an output frame; None inside the card span."""

    if not 0 <= output_frame < OUTPUT_FRAMES:
        raise ChapterCardPlanError(
            "output-frame-range", f"output frame {output_frame} outside [0,{OUTPUT_FRAMES})"
        )
    if frame_kind(output_frame) == "card":
        return None
    if output_frame < RECORD_FRAME:
        return output_frame
    return output_frame - CARD_FRAMES


def subtitle_item_count(plan_v3_path: Path) -> int:
    try:
        document: object = json.loads(plan_v3_path.read_bytes())
    except OSError as error:
        raise ChapterCardPlanError(
            "plan-unreadable", f"cannot read review plan: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise ChapterCardPlanError("plan-invalid", f"review plan is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise ChapterCardPlanError("plan-invalid", "review plan is not an object")
    plan_body: object = document.get("plan")
    items: object = plan_body.get("items") if isinstance(plan_body, dict) else None
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and "kind" in item for item in items
    ):
        raise ChapterCardPlanError("plan-invalid", "review plan items are malformed")
    return sum(1 for item in items if item["kind"] == "subtitle")


__all__ = [
    "BASE_IR_SHA256",
    "BASE_PLAN_SHA256",
    "BOUNDARY_SOURCE_FRAME",
    "BYTES_PER_SAMPLE_FRAME",
    "CANDIDATE_ID",
    "CANVAS_H",
    "CANVAS_W",
    "CARD_FRAMES",
    "EPISODE_ID",
    "FONT_FACE",
    "FONT_SHA256",
    "FPS",
    "INSERT_SAMPLE",
    "LEFT_ITEM_ID",
    "MODEL_ID",
    "OPERATOR_AUDIO_STATEMENT",
    "OPERATOR_TITLE_STATEMENTS",
    "OUTPUT_FRAMES",
    "OUTPUT_PCM_BYTES",
    "OUTPUT_SAMPLES",
    "PREFIX_BYTES",
    "RECORD_FRAME",
    "RIGHT_ITEM_ID",
    "SAMPLES_PER_FRAME",
    "SAMPLE_RATE",
    "SILENCE_BYTES",
    "SILENCE_SAMPLES",
    "SOURCE_FRAMES",
    "SOURCE_PCM_BYTES",
    "SOURCE_PCM_SHA256",
    "SOURCE_SAMPLES",
    "SOURCE_VIDEO_SHA256",
    "STEREO_CHANNELS",
    "TITLE",
    "VIDEO_BITRATE",
    "VIDEO_TIMESCALE",
    "ApprovedCard",
    "ChapterCardPlanError",
    "build_output_pcm",
    "frame_kind",
    "load_approved_card",
    "source_frame_for_output",
    "split_output_pcm",
    "subtitle_item_count",
]
