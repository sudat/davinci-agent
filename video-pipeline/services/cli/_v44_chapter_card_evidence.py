"""Assembly of the chapter-card insertion evidence document."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli import _v44_chapter_card_files as files
from services.cli._v44_chapter_card_plan import (
    BASE_PLAN_SHA256,
    CARD_FRAMES,
    EPISODE_ID,
    OPERATOR_AUDIO_STATEMENT,
    OPERATOR_TITLE_STATEMENTS,
    RECORD_FRAME,
    SOURCE_FRAMES,
    SOURCE_VIDEO_SHA256,
    TITLE,
    VIDEO_BITRATE,
)
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.cli._v44_chapter_card_pair_qa import FramePairResult
    from services.cli._v44_chapter_card_plan import ApprovedCard
    from services.cli._v44_chapter_card_preflight import FontFacts
    from services.cli._v44_chapter_card_probe import MasterFacts
    from services.cli._v44_chapter_card_qa import CardSpanReport
    from services.preview.tools import PinnedTools

SCHEMA_VERSION: Final = "v44-chapter-card-insertion-evidence-v1"
TOOLCHAIN_LOCK: Final = "config/toolchains/phase-0c-v2.json"


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """Every measured fact the evidence document records."""

    tools: PinnedTools
    source: Path
    proposal: Path
    plan_v3: Path
    master: Path
    rendered_master: Path
    mux_argv: tuple[str, ...]
    facts: MasterFacts
    card_span: CardSpanReport
    pairs: tuple[FramePairResult, ...]
    audio: dict[str, object]
    card: ApprovedCard
    font: Path
    font_facts: FontFacts
    font_size_px: int
    subtitle_items: int
    protected_before: dict[str, str]
    protected_after: dict[str, str]


def build_evidence(request: EvidenceRequest) -> bytes:
    """The canonical evidence JSON bytes for one fully gated insertion run."""

    span_end = RECORD_FRAME + CARD_FRAMES
    document = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": EPISODE_ID,
        "operator_approval": {
            "selected_title": TITLE,
            "title_statements": list(OPERATOR_TITLE_STATEMENTS),
            "audio_statement": OPERATOR_AUDIO_STATEMENT,
        },
        "proposal": {
            "path": str(request.proposal),
            "sha256": sha256_file(request.proposal),
            "candidate_id": request.card.candidate_id,
            "model_id": request.card.model_id,
            "base_plan_sha256": request.card.base_plan_sha256,
            "base_ir_sha256": request.card.base_ir_sha256,
            "boundary": {
                "left": request.card.left_item_id,
                "right": request.card.right_item_id,
                "record_frame": request.card.record_frame,
                "source_frame": request.card.source_frame,
                "card_frames": request.card.card_frames,
            },
        },
        "review_plan": {
            "path": str(request.plan_v3),
            "sha256": BASE_PLAN_SHA256,
            "subtitle_items": request.subtitle_items,
        },
        "source": {
            "path": str(request.source),
            "sha256": SOURCE_VIDEO_SHA256,
            "video_frames": SOURCE_FRAMES,
        },
        "output": {
            "path": str(request.master),
            "sha256": sha256_file(request.rendered_master),
            "size_bytes": files.file_size(request.rendered_master),
            "video_codec": request.facts.video_codec,
            "video_time_base": request.facts.video_time_base,
            "video_frames": request.facts.video_frames,
            "avg_frame_rate": request.facts.avg_frame_rate,
            "audio_codec": request.facts.audio_codec,
            "audio_samples": request.facts.audio_samples,
        },
        "render": {
            "mux_argv": list(request.mux_argv),
            "video_encoder": "h264_videotoolbox",
            "video_bitrate": VIDEO_BITRATE,
            "video_timescale": request.facts.video_timescale,
            "frame_mapping": f"output i < {RECORD_FRAME} -> source i; "
            f"[{RECORD_FRAME},{span_end}) -> card; i >= {span_end} -> source i-{CARD_FRAMES}",
        },
        "audio_proof": request.audio,
        "visual_qa": {
            "card_span": dict(request.card_span),
            "frame_pairs": [asdict(pair) for pair in request.pairs],
        },
        "tools": {
            "ffmpeg": str(request.tools.ffmpeg),
            "ffmpeg_sha256": request.tools.ffmpeg_sha256,
            "ffprobe": str(request.tools.ffprobe),
            "ffprobe_sha256": request.tools.ffprobe_sha256,
            "toolchain_lock": TOOLCHAIN_LOCK,
            "font": str(request.font),
            "font_sha256": request.font_facts.sha256,
            "font_face": list(request.font_facts.face),
            "font_size_px": request.font_size_px,
        },
        "protection": {
            "before": request.protected_before,
            "after": request.protected_after,
            "unchanged": True,
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode()


__all__ = ["EvidenceRequest", "build_evidence"]
