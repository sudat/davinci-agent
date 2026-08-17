"""Minimum visual checks + deterministic contact sheets: designed-video goldens.

Every expectation below is PRE-REGISTERED from the SYNTHESIS parameters and
the frozen visual constants — nothing is measured-then-asserted. The designed
fixture is 80 frames of CFR30 FFV1 video built from five pinned-lavfi
segments:

    [0,10)   color=black            -> luma mean 0   (black span)
    [10,30)  smptebars              -> luma mean_m 367, Laplacian energy 1545 (sharp)
    [30,50)  smptebars,gblur=sigma=8-> same mean, Laplacian energy 113 (softened)
    [50,60)  color=white            -> luma mean_m 1000, clipped-high 1000 permille
    [60,80)  smptebars              -> sharp again

Pinned-ffmpeg availability was probed up front: gblur / smptebars / color /
concat / scale / format ARE in the frozen filter set; ``eq`` and any PNG
encoder are NOT, so overexposure is synthesized as a white color segment and
contact sheets are written as pure-Python zlib PNGs (byte-deterministic).

Frozen metric consequences (measured once, then locked as margins):
- scene-change mean-abs-luma-diff at boundaries 10/50/60 is 367/632/632
  permille (>= 100 threshold) while the same-mean boundary 30 is 31 permille
  and must NOT fire — the minimal mean-diff rule sees blur, not scenes;
- blur Laplacian mean-square is 1545 (sharp) vs 113 (softened) with the
  frozen threshold 500 sitting between at >=3x margin on both sides.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

import services.analyze.contact_sheet as sheet_module
import services.analyze.visual_analysis as analysis_module
import services.analyze.visual_checks as checks_module
from services.analyze.analysis_models import AnalyzeRequestError
from services.analyze.contact_sheet import encode_gray_png
from services.analyze.visual_analysis import VisualAnalysisRequest, analyze_visual
from services.analyze.visual_constants import (
    ANALYZER_VERSION,
    BLACK_MAX_MEAN_M,
    BLACK_RULE_ID,
    BLUR_MAX_LAPLACIAN_MEAN_SQUARE,
    BLUR_RULE_ID,
    DECODE_FILTER,
    DECODE_TIMEOUT_SEC,
    EXPOSURE_RULE_ID,
    MAX_DECODE_FRAMES,
    SCENE_MIN_MEAN_DIFF_M,
    SCENE_RULE_ID,
    SHEET_CADENCE_FRAMES,
    SHEET_COLS,
    SHEET_RULE_ID,
    frozen_constants_hash,
)
from services.analyze.visual_decode import ensure_decode_budget, probe_video_facts
from services.analyze.visual_models import (
    FORBIDDEN_SELECTION_EXPORTS,
    VisualAnalysisArtifact,
    VisualDecodeError,
    decode_binding_hash,
    visual_content_hash,
)
from services.contracts.edit_plan_0c import EditPlan0C
from services.foundation_io import sha256_file
from services.toolchain.models import Phase1TechnicalToolchainLock, load_lock

# --- pre-registered golden values (from synthesis parameters only) ---
GOLDEN_FRAME_COUNT = 80
GOLDEN_BLACK_SPANS = ((0, 10),)
GOLDEN_BLUR_SPANS = ((30, 50),)
GOLDEN_EXPOSURE_SPANS = (("over", 50, 60),)
GOLDEN_SCENE_BOUNDARIES = (10, 50, 60)  # boundary 30 is same-mean: must NOT fire
GOLDEN_SCENE_DIFF_M = {10: 367, 50: 632, 60: 632}
GOLDEN_MEAN_M = {"black": 0, "bars": 367, "white": 1000}
GOLDEN_SHARP_LAP2 = 1545
GOLDEN_BLURRED_LAP2 = 113
GOLDEN_SHEET_THUMB_COUNT = 8  # frames 0,10,...,70 at cadence 10
GOLDEN_SHEET_SIZE = (320, 72)  # 5 cols x 64 px, 2 rows x 36 px

SEGMENT_GRAPHS = (
    ("color=black:s=320x180:r=30", 10),
    ("smptebars=s=320x180:r=30", 20),
    ("smptebars=s=320x180:r=30,gblur=sigma=8", 20),
    ("color=white:s=320x180:r=30", 10),
)


@dataclass(frozen=True, slots=True)
class VisualTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_sha256: str


@pytest.fixture(scope="session")
def visual_tools() -> VisualTools:
    lock = load_lock(Path("config/toolchains/phase-1-technical-v1.json"))
    assert isinstance(lock, Phase1TechnicalToolchainLock)
    tools = VisualTools(
        ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(lock.ffmpeg.ffprobe.path),
        ffmpeg_sha256=lock.ffmpeg.ffmpeg.sha256,
    )
    if not tools.ffmpeg.is_file() or not tools.ffprobe.is_file():
        pytest.skip("pinned ffmpeg/ffprobe not bootstrapped")
    return tools


def _run_ffmpeg(ffmpeg: Path, argv: tuple[str, ...], out: Path) -> None:
    result = subprocess.run(
        (str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *argv, str(out)),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert out.is_file()


@pytest.fixture(scope="session")
def designed_avi(visual_tools: VisualTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Five lavfi segments concatenated losslessly (FFV1) into 80 CFR30 frames."""
    root = tmp_path_factory.mktemp("todo35-visual")
    segment_paths = []
    for index, (graph, frames) in enumerate(SEGMENT_GRAPHS):
        segment = root / f"seg{index}.avi"
        _run_ffmpeg(
            visual_tools.ffmpeg,
            ("-f", "lavfi", "-i", graph, "-frames:v", str(frames), "-c:v", "ffv1"),
            segment,
        )
        segment_paths.append(segment)
    sharp_again = root / "seg4.avi"
    shutil.copyfile(segment_paths[1], sharp_again)
    segment_paths.append(sharp_again)
    chain = "".join(f"[{index}:v]" for index in range(len(segment_paths)))
    designed = root / "designed.avi"
    _run_ffmpeg(
        visual_tools.ffmpeg,
        (
            *[part for segment in segment_paths for part in ("-i", str(segment))],
            "-filter_complex",
            f"{chain}concat=n={len(segment_paths)}:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "ffv1",
        ),
        designed,
    )
    facts = probe_video_facts(visual_tools.ffprobe, designed)
    assert (facts.width, facts.height, facts.frame_count) == (320, 180, GOLDEN_FRAME_COUNT)
    assert (facts.rate_num, facts.rate_den) == (30, 1)
    assert (facts.time_base_num, facts.time_base_den) == (1, 30)
    return designed


def _analyze(designed_avi: Path, tmp_path: Path, *, fixture_only: bool = True):
    return analyze_visual(
        VisualAnalysisRequest(
            media_path=str(designed_avi),
            media_sha256=sha256_file(designed_avi),
            fixture_only=fixture_only,
        ),
        work_dir=tmp_path / "work",
    )


def test_minimum_checks_on_designed_fixture(designed_avi: Path, tmp_path: Path) -> None:
    result = _analyze(designed_avi, tmp_path)
    artifact = result.artifact
    media_sha = sha256_file(designed_avi)
    binding_sha = decode_binding_hash(artifact.media)

    assert artifact.evidence_only is True
    assert artifact.fixture_only is True
    assert artifact.producer.name == "analyze-visual"
    assert artifact.producer.version == ANALYZER_VERSION
    assert artifact.media.media_sha256 == media_sha
    assert artifact.media.decode_filter == DECODE_FILTER
    assert artifact.media.ffmpeg_sha256 == artifact.media.ffmpeg_sha256  # present + pinned
    assert len(artifact.frames) == GOLDEN_FRAME_COUNT
    assert [fact.frame_index for fact in artifact.frames] == list(range(GOLDEN_FRAME_COUNT))
    assert artifact.frames[30].pts.num == 30
    assert artifact.frames[30].pts.den == 1

    # frame facts match synthesis params exactly
    assert artifact.frames[0].mean_luma_m == GOLDEN_MEAN_M["black"]
    assert artifact.frames[20].mean_luma_m == GOLDEN_MEAN_M["bars"]
    assert artifact.frames[55].mean_luma_m == GOLDEN_MEAN_M["white"]
    assert artifact.frames[20].laplacian_mean_square == GOLDEN_SHARP_LAP2
    assert artifact.frames[40].laplacian_mean_square == GOLDEN_BLURRED_LAP2
    # frozen threshold sits between sharp and softened with wide margin
    assert GOLDEN_SHARP_LAP2 >= 2 * BLUR_MAX_LAPLACIAN_MEAN_SQUARE
    assert GOLDEN_BLURRED_LAP2 <= BLUR_MAX_LAPLACIAN_MEAN_SQUARE // 2

    assert [(span.span.start_frame, span.span.end_frame) for span in artifact.black_spans] == [
        GOLDEN_BLACK_SPANS[0]
    ]
    assert [(span.span.start_frame, span.span.end_frame) for span in artifact.blur_spans] == [
        GOLDEN_BLUR_SPANS[0]
    ]
    assert [
        (s.direction, s.span.start_frame, s.span.end_frame) for s in artifact.exposure_spans
    ] == [GOLDEN_EXPOSURE_SPANS[0]]
    detected = tuple(change.boundary_frame for change in artifact.scene_changes)
    assert detected == GOLDEN_SCENE_BOUNDARIES
    for change in artifact.scene_changes:
        assert change.mean_diff_m == GOLDEN_SCENE_DIFF_M[change.boundary_frame]
        assert change.mean_diff_m >= SCENE_MIN_MEAN_DIFF_M

    # provenance + confidence + version on EVERY result
    rule_ids = {BLACK_RULE_ID, BLUR_RULE_ID, EXPOSURE_RULE_ID, SCENE_RULE_ID}
    everything = (
        *artifact.scene_changes,
        *artifact.black_spans,
        *artifact.blur_spans,
        *artifact.exposure_spans,
    )
    assert everything
    for item in everything:
        assert 0 <= item.confidence <= 1000
        assert item.provenance.analyzer_version == ANALYZER_VERSION
        assert item.provenance.rule_id in rule_ids
        assert item.provenance.decode_binding_sha256 == binding_sha
        assert media_sha in item.provenance.input_artifact_hashes
    for sheet in artifact.sheets:
        assert sheet.provenance.rule_id == SHEET_RULE_ID

    # black frames are never claimed as blur or exposure (rule ordering)
    for span in artifact.blur_spans:
        assert span.span.start_frame >= 10
    for span in artifact.exposure_spans:
        assert span.span.start_frame >= 10


def test_contact_sheet_deterministic_png(designed_avi: Path, tmp_path: Path) -> None:
    first = _analyze(designed_avi, tmp_path / "a")
    second = _analyze(designed_avi, tmp_path / "b")
    assert first.artifact.sheets == second.artifact.sheets
    sheet = first.artifact.sheets[0]
    assert len(sheet.frame_indexes) == GOLDEN_SHEET_THUMB_COUNT
    assert sheet.frame_indexes == tuple(range(0, GOLDEN_FRAME_COUNT, SHEET_CADENCE_FRAMES))
    assert (sheet.cols, sheet.rows) == (SHEET_COLS, 2)
    assert sheet.rebuildable is True
    first_bytes = Path(first.sheet_paths[0]).read_bytes()
    assert first_bytes == Path(second.sheet_paths[0]).read_bytes()
    assert sha256_file(Path(first.sheet_paths[0])) == sheet.sha256

    assert first_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", first_bytes[16:24])
    assert (width, height) == GOLDEN_SHEET_SIZE

    # pure-Python encoder is standalone deterministic (same rows -> same bytes)
    assert encode_gray_png(2, 1, b"\x00\xff") == encode_gray_png(2, 1, b"\x00\xff")


def test_analysis_reproducible_content_hash(designed_avi: Path, tmp_path: Path) -> None:
    first = _analyze(designed_avi, tmp_path / "a")
    second = _analyze(designed_avi, tmp_path / "b")
    assert first.artifact == second.artifact
    assert Path(first.artifact_path).read_bytes() == Path(second.artifact_path).read_bytes()


def test_untraceable_frame_rejected(designed_avi: Path, tmp_path: Path) -> None:
    result = _analyze(designed_avi, tmp_path)
    payload = result.artifact.model_dump(mode="json")

    # 1) a span claiming a frame beyond the decode evidence is rejected
    beyond = dict(payload)
    beyond["scene_changes"] = [
        {
            **payload["scene_changes"][0],
            "boundary_frame": payload["media"]["facts"]["frame_count"] + 19,
            "span": {"start_frame": 99, "end_frame": 100},
            "decode_frame_count": payload["media"]["facts"]["frame_count"],
        }
    ]
    with pytest.raises(ValidationError):
        VisualAnalysisArtifact.model_validate_json(json.dumps(beyond))

    # 2) a span inflating its own decode evidence drifts from the binding
    inflated = dict(payload)
    inflated["black_spans"] = [
        {**payload["black_spans"][0], "decode_frame_count": 900}
    ]
    with pytest.raises(ValidationError):
        VisualAnalysisArtifact.model_validate_json(json.dumps(inflated))

    # 3) dropping provenance entirely leaves the frame untraceable
    orphan = dict(payload)
    orphan["blur_spans"] = [
        {key: value for key, value in payload["blur_spans"][0].items() if key != "provenance"}
    ]
    with pytest.raises(ValidationError):
        VisualAnalysisArtifact.model_validate_json(json.dumps(orphan))


def test_stale_media_binding_rejected(designed_avi: Path, tmp_path: Path) -> None:
    result = _analyze(designed_avi, tmp_path)
    payload = result.artifact.model_dump(mode="json")
    drifted = dict(payload)
    drifted["media"] = {**payload["media"], "media_sha256": "e" * 64}
    with pytest.raises(ValidationError):
        VisualAnalysisArtifact.model_validate_json(json.dumps(drifted))
    # sanity: the untouched payload round-trips
    assert VisualAnalysisArtifact.model_validate_json(json.dumps(payload)) == result.artifact


def test_corrupt_media_structured_decode_failure(
    visual_tools: VisualTools, tmp_path: Path
) -> None:
    corrupt = tmp_path / "corrupt.avi"
    corrupt.write_bytes(b"RIFFgarbage-not-a-video" * 64)
    request = VisualAnalysisRequest(
        media_path=str(corrupt), media_sha256=sha256_file(corrupt), fixture_only=True
    )
    with pytest.raises(VisualDecodeError) as failed:
        analyze_visual(request, work_dir=tmp_path / "work")
    assert failed.value.label == "corrupt_decode"
    assert not (tmp_path / "work" / "visual-analysis-artifact.json").exists()


def test_decode_is_bounded() -> None:
    ensure_decode_budget(MAX_DECODE_FRAMES)  # at the limit: allowed
    with pytest.raises(AnalyzeRequestError):
        ensure_decode_budget(MAX_DECODE_FRAMES + 1)
    assert DECODE_TIMEOUT_SEC == 120


def test_frozen_constants_pinned() -> None:
    assert BLACK_MAX_MEAN_M == 50
    assert BLUR_MAX_LAPLACIAN_MEAN_SQUARE == 500
    assert SCENE_MIN_MEAN_DIFF_M == 100
    assert frozen_constants_hash() == frozen_constants_hash()
    assert frozen_constants_hash() != "0" * 64


def test_no_selection_export_surface(designed_avi: Path, tmp_path: Path) -> None:
    for module in (sheet_module, analysis_module, checks_module):
        exported = set(module.__all__)
        assert set(FORBIDDEN_SELECTION_EXPORTS).isdisjoint(exported)
    assert "selection" not in VisualAnalysisArtifact.model_fields
    plan = object.__new__(EditPlan0C)
    with pytest.raises(AnalyzeRequestError, match="Edit Plan"):
        analyze_visual(plan, work_dir=tmp_path / "w")  # type: ignore[arg-type]


def test_visual_content_hash_binds_every_field(designed_avi: Path, tmp_path: Path) -> None:
    result = _analyze(designed_avi, tmp_path)
    artifact = result.artifact
    assert artifact.content_hash == visual_content_hash(
        artifact.media,
        artifact.frames,
        artifact.scene_changes,
        artifact.black_spans,
        artifact.blur_spans,
        artifact.exposure_spans,
        artifact.sheets,
        fixture_only=artifact.fixture_only,
    )


def _scope_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "services.policy.check_scope", *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_prohibited_analyzer_import_flagged(tmp_path: Path) -> None:
    analyze_dir = tmp_path / "services" / "analyze"
    analyze_dir.mkdir(parents=True)
    (analyze_dir / "__init__.py").write_text("")
    (analyze_dir / "visual_motion.py").write_text(
        "import cv2\nfrom face_motion_tracker import track\n"
    )
    (analyze_dir / "plate_clustering.py").write_text("import services.contracts.primitives\n")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bad-keys.json").write_text('{"motion_analysis": {}, "face_tracking": {}}\n')

    flagged = _scope_cli("--phase", "1", "--root", str(tmp_path))
    assert flagged.returncode == 1
    output = flagged.stdout + flagged.stderr
    assert "visual_motion.py" in output
    assert "cv2" in output
    assert "plate_clustering.py" in output
    assert "face_motion_tracker" in output
    assert "motion_analysis" in output
    assert "face_tracking" in output

    clean = _scope_cli("--phase", "1")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "scope-clean" in clean.stdout

    unsupported = _scope_cli("--phase", "4")
    assert unsupported.returncode == 2
