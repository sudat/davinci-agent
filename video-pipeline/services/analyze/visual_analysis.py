"""Entry point of the minimum visual analyzer (Todo 35).

Read-only evidence producer: probes and decodes the declared Edit-Source
video with the pinned, hash-verified ffmpeg/ffprobe, runs the four minimum
checks plus the deterministic contact sheet, and writes one canonical
``VisualAnalysisArtifact``. Outputs are evidence-only — they are never
selection inputs, and passing an Edit Plan is refused outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.analyze.analysis_models import AnalyzeRequestError, refuse_edit_plan
from services.analyze.audio_probe import resolve_audio_tools
from services.analyze.contact_sheet import build_contact_sheet
from services.analyze.visual_checks import (
    detect_black_spans,
    detect_blur_spans,
    detect_exposure_spans,
    detect_scene_changes,
)
from services.analyze.visual_constants import (
    ANALYZER_VERSION,
    ARTIFACT_NAME,
    ARTIFACT_TYPE,
    SCHEMA_VERSION,
    SHEET_NAME,
)
from services.analyze.visual_decode import bind_decode, probe_video_facts
from services.analyze.visual_metrics import compute_frame_facts
from services.analyze.visual_models import (
    VISUAL_PRODUCER,
    VisualAnalysisArtifact,
    visual_content_hash,
    visual_input_refs,
)
from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

DEFAULT_LOCK_PATH = Path("config/toolchains/phase-1-technical-v1.json")


class VisualAnalysisRequest(StrictModel):
    media_path: str
    media_sha256: Sha256
    fixture_only: bool = False


@dataclass(frozen=True, slots=True)
class VisualAnalysisResult:
    artifact: VisualAnalysisArtifact
    artifact_path: str
    sheet_paths: tuple[str, ...]


def analyze_visual(
    request: object,
    *,
    work_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> VisualAnalysisResult:
    refuse_edit_plan(request)
    if not isinstance(request, VisualAnalysisRequest):
        raise AnalyzeRequestError("analyze_visual accepts a VisualAnalysisRequest only")
    media = Path(request.media_path)
    if not media.is_file() or sha256_file(media) != request.media_sha256:
        raise AnalyzeRequestError(f"media missing or hash drift: {media}")

    tools = resolve_audio_tools(lock_path)
    facts = probe_video_facts(tools.ffprobe, media)
    binding, frames = bind_decode(media, request.media_sha256, facts, tools=tools)
    input_hashes = (request.media_sha256,)

    frame_facts = compute_frame_facts(frames)
    scene_changes = detect_scene_changes(frame_facts, binding, input_hashes)
    black_spans = detect_black_spans(frame_facts, binding, input_hashes)
    blur_spans = detect_blur_spans(frame_facts, binding, input_hashes)
    exposure_spans = detect_exposure_spans(frame_facts, binding, input_hashes)

    work_dir.mkdir(parents=True, exist_ok=True)
    sheet_path = work_dir / SHEET_NAME
    sheet = build_contact_sheet(frames, binding, input_hashes, out_path=sheet_path)

    content_hash = visual_content_hash(
        binding,
        frame_facts,
        scene_changes,
        black_spans,
        blur_spans,
        exposure_spans,
        (sheet,),
        fixture_only=request.fixture_only,
    )
    artifact = VisualAnalysisArtifact(
        artifact_id=f"analysis-{content_hash[:16]}",
        artifact_type=ARTIFACT_TYPE,
        schema_version=SCHEMA_VERSION,
        content_hash=content_hash,
        producer=VISUAL_PRODUCER,
        inputs=visual_input_refs(request.media_sha256),
        media=binding,
        frames=frame_facts,
        scene_changes=scene_changes,
        black_spans=black_spans,
        blur_spans=blur_spans,
        exposure_spans=exposure_spans,
        sheets=(sheet,),
        fixture_only=request.fixture_only,
    )
    artifact_path = work_dir / ARTIFACT_NAME
    atomic_write(artifact_path, canonical_model_bytes(artifact))
    return VisualAnalysisResult(
        artifact=artifact,
        artifact_path=str(artifact_path),
        sheet_paths=(str(sheet_path),),
    )


__all__ = [
    "ANALYZER_VERSION",
    "VisualAnalysisRequest",
    "VisualAnalysisResult",
    "analyze_visual",
]
