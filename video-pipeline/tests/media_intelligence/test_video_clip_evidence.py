"""T4 short-clip evidence + deterministic extraction — Tier A (real ffmpeg).

Proves the T4 evidence path with the PINNED phase-1 ffmpeg/ffprobe (no
mocked extraction): one exact half-open Edit Source interval yields an
audio-bearing Gemini clip and an audio-free GLM clip (``-an``), both hashed
as local ``file://`` real evidence with deterministic names; the extraction
uses the EXACT rational frame rate (30000/1001 — the T9 notepad lesson:
never test rate math at 30/1 only); requested == analyzed ranges are exact
Edit Source frames; and every named rejection (synthetic/nonlocal/relative
refs, missing file, directory, hash mismatch, empty media, GLM audio,
out-of-source windows) is typed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from services.analyze.audio_probe import PinnedAudioTools, resolve_audio_tools
from services.analyze.visual_decode import decode_luma_window, probe_video_facts
from services.foundation_io import sha256_file
from services.media_intelligence.moment_review import ReviewWindow
from services.media_intelligence.moment_review_real import AssessmentEvidenceBundle
from services.media_intelligence.video_clip_evidence import (
    VideoClipError,
    VideoClipEvidence,
    verify_local_clip,
)
from services.media_intelligence.video_clip_extraction import ClipExtractor

EPISODE_ID = "ep-t4-clip"
WINDOW: Final = ReviewWindow(start_frame=7, end_frame=37)  # 30 frames, half-open


# ------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def pinned_tools() -> PinnedAudioTools:
    try:
        return resolve_audio_tools()
    except (OSError, ValueError) as error:
        pytest.skip(f"pinned phase-1 ffmpeg not bootstrapped: {error}")


@pytest.fixture(scope="session")
def ntsc_av_source(
    pinned_tools: PinnedAudioTools, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Real mp4: testsrc2 320x240 @ EXACT 30000/1001, 2 s, h264 + aac tone."""

    media = tmp_path_factory.mktemp("t4-clips") / "ntsc-av.mp4"
    result = subprocess.run(
        (
            str(pinned_tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30000/1001:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    facts = probe_video_facts(pinned_tools.ffprobe, media)
    assert (facts.rate_num, facts.rate_den) == (30000, 1001)  # never approximated
    assert facts.frame_count >= 59
    return media


def _streams(ffprobe: Path, media: Path) -> list[dict[str, object]]:
    result = subprocess.run(
        (
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-400:]
    streams: list[dict[str, object]] = json.loads(result.stdout)["streams"]
    return streams


def _video_frames(ffprobe: Path, media: Path) -> int:
    video = [stream for stream in _streams(ffprobe, media) if stream.get("codec_type") == "video"]
    assert len(video) == 1
    nb_frames = video[0].get("nb_frames")
    assert isinstance(nb_frames, str)
    assert nb_frames.isdigit()
    return int(nb_frames)


def _audio_streams(ffprobe: Path, media: Path) -> int:
    return sum(1 for stream in _streams(ffprobe, media) if stream.get("codec_type") == "audio")


# ------------------------------------------------------------ extraction


def test_one_interval_yields_audio_gemini_and_silent_glm_clips(
    pinned_tools: PinnedAudioTools, ntsc_av_source: Path, tmp_path: Path
) -> None:
    """Given: one exact half-open interval on the NTSC AV source; Then: the
    Gemini clip keeps audio, the GLM clip has ZERO audio streams (ffprobe,
    not flags), both carry exactly the requested frames, and both hashes
    match the bytes on disk."""

    extractor = ClipExtractor(media_path=ntsc_av_source, workspace_dir=tmp_path)
    pair = extractor.extract(WINDOW)

    media_sha = sha256_file(ntsc_av_source)
    expected_av = tmp_path / "moment-review-clips" / f"clip-av-{media_sha[:12]}-000007-000037.mp4"
    expected_v = tmp_path / "moment-review-clips" / f"clip-v-{media_sha[:12]}-000007-000037.mp4"
    assert Path(pair.gemini.ref.removeprefix("file://")) == expected_av
    assert Path(pair.glm.ref.removeprefix("file://")) == expected_v

    for clip, audio_streams in ((pair.gemini, 1), (pair.glm, 0)):
        path = Path(clip.ref.removeprefix("file://"))
        assert path.is_file()
        assert _video_frames(pinned_tools.ffprobe, path) == 30  # [7, 37) exact
        assert _audio_streams(pinned_tools.ffprobe, path) == audio_streams
        assert clip.sha256 == sha256_file(path)
        assert clip.requested_range == WINDOW
        assert clip.analyzed_range == WINDOW
    assert pair.gemini.audio_present is True  # source interval audio retained
    assert pair.glm.audio_present is False
    assert pair.gemini.duration_seconds == pytest.approx(1.001, rel=1e-6)  # 30*1001/30000
    assert pair.glm.duration_seconds == pytest.approx(1.001, rel=1e-6)


def test_extraction_is_deterministic_in_naming_and_rational_in_time(
    pinned_tools: PinnedAudioTools, ntsc_av_source: Path, tmp_path: Path
) -> None:
    """Given: the same window extracted twice; Then: identical output names
    (rebuildable evidence) — and the seek honors the EXACT rational rate: a
    tail window [59, 60) opens on frame 59, not frame 0 (the T9 num-only
    rate bug would seek 59/30000s ≈ 2ms and copy the clip's head)."""

    extractor = ClipExtractor(media_path=ntsc_av_source, workspace_dir=tmp_path)
    first = extractor.extract(WINDOW)
    second = extractor.extract(WINDOW)
    assert first.gemini.ref == second.gemini.ref
    assert first.glm.ref == second.glm.ref

    tail = ReviewWindow(start_frame=58, end_frame=59)
    clip = extractor.extract(tail).gemini
    clip_path = Path(clip.ref.removeprefix("file://"))
    assert clip.duration_seconds == pytest.approx(1001 / 30000, rel=1e-6)

    tools = resolve_audio_tools()
    clip_facts = probe_video_facts(tools.ffprobe, clip_path)
    src_facts = probe_video_facts(tools.ffprobe, ntsc_av_source)
    observed = decode_luma_window(clip_path, clip_facts, tools=tools, start_frame=0, end_frame=1)[
        0
    ].luma
    frame_58 = decode_luma_window(
        ntsc_av_source, src_facts, tools=tools, start_frame=58, end_frame=59
    )[0].luma
    frame_0 = decode_luma_window(
        ntsc_av_source, src_facts, tools=tools, start_frame=0, end_frame=1
    )[0].luma

    def _mean_abs_diff(left: bytes, right: bytes) -> float:
        return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)

    near_58 = _mean_abs_diff(observed, frame_58)
    near_0 = _mean_abs_diff(observed, frame_0)
    assert near_58 < near_0, (
        f"tail clip should open on source frame 58 (diff {near_58:.2f}), "
        f"not frame 0 (diff {near_0:.2f}) — rational-rate seek regression"
    )


def test_extractor_rejects_empty_and_out_of_source_windows(
    ntsc_av_source: Path, tmp_path: Path
) -> None:
    extractor = ClipExtractor(media_path=ntsc_av_source, workspace_dir=tmp_path)
    with pytest.raises(VideoClipError, match="forward"):
        extractor.extract(ReviewWindow(start_frame=7, end_frame=7))
    with pytest.raises(VideoClipError, match="frame stream"):
        extractor.extract(ReviewWindow(start_frame=55, end_frame=95))


# ------------------------------------------------------------ review scaling
# MEASURED 2026-08-30 (T11 pre-flight, pinned ffmpeg 7.1.1): the first
# local-map window [0,3600) extracted at the 3840x2160 mezzanine resolution
# is 800,410,980 bytes — 53x over the 15 MiB provider media bound and 40x
# over the 20 MiB total-request bound. Review clips are MODEL-FACING
# evidence, so wide sources are deterministically review-scaled; at
# 960x540 / 700k video / 64k audio the worst-case 3600-frame window is
# 11,614,068 bytes (base64 ≈ 15.5 MB, inside both bounds).


@pytest.fixture(scope="session")
def wide_av_source(
    pinned_tools: PinnedAudioTools, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Real mp4: testsrc2 1920x1080 @ 30/1, 2 s, h264 + aac tone."""

    media = tmp_path_factory.mktemp("t11-wide") / "wide-av.mp4"
    result = subprocess.run(
        (
            str(pinned_tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    facts = probe_video_facts(pinned_tools.ffprobe, media)
    assert facts.width == 1920
    assert facts.frame_count >= 59
    return media


def _clip_width(ffprobe: Path, media: Path) -> int:
    video = [stream for stream in _streams(ffprobe, media) if stream.get("codec_type") == "video"]
    assert len(video) == 1
    width = video[0].get("width")
    assert isinstance(width, int)
    return width


def test_wide_source_clips_are_review_scaled_and_small_sources_untouched(
    pinned_tools: PinnedAudioTools,
    wide_av_source: Path,
    ntsc_av_source: Path,
    tmp_path: Path,
) -> None:
    """Given: a >review-width source; When: extracting one interval; Then:
    every clip's width is capped at the review width with an even height and
    the exact frame count, while a small source keeps its native size."""

    pair = ClipExtractor(media_path=wide_av_source, workspace_dir=tmp_path).extract(WINDOW)
    for clip in (pair.gemini, pair.glm):
        path = Path(clip.ref.removeprefix("file://"))
        video = [
            stream
            for stream in _streams(pinned_tools.ffprobe, path)
            if stream.get("codec_type") == "video"
        ]
        assert len(video) == 1
        width = video[0].get("width")
        height = video[0].get("height")
        assert isinstance(width, int)
        assert isinstance(height, int)
        assert width <= 960, "review clip must fit the provider inline bound"
        assert height % 2 == 0  # yuv420p needs even dims
        assert _video_frames(pinned_tools.ffprobe, path) == 30  # [7, 37) exact

    small = ClipExtractor(media_path=ntsc_av_source, workspace_dir=tmp_path).extract(WINDOW)
    assert _clip_width(pinned_tools.ffprobe, Path(small.gemini.ref.removeprefix("file://"))) == 320


# ------------------------------------------------------------ evidence model


def _evidence(**overrides: object) -> VideoClipEvidence:
    fields: dict[str, object] = {
        "ref": "file:///var/empty/t4/clip.mp4",
        "sha256": "a" * 64,
        "requested_range": WINDOW,
        "analyzed_range": WINDOW,
        "audio_present": False,
        "duration_seconds": 1.001,
    }
    fields.update(overrides)
    return VideoClipEvidence.model_validate(fields)


def test_evidence_model_requires_local_absolute_file_refs() -> None:
    valid = _evidence()
    assert valid.audio_present is False
    for bad_ref in (
        "synthetic://window/clip.mp4",
        "https://media.example.invalid/clip.mp4",
        "file://relative/clip.mp4",
        "clip.mp4",
    ):
        with pytest.raises(ValidationError, match="file://"):
            _evidence(ref=bad_ref)


def test_evidence_model_requires_analyzed_equals_requested_and_forward() -> None:
    with pytest.raises(ValidationError, match="analyzed"):
        _evidence(analyzed_range=ReviewWindow(start_frame=7, end_frame=36))
    with pytest.raises(ValidationError, match="forward"):
        _evidence(
            requested_range=ReviewWindow(start_frame=7, end_frame=7),
            analyzed_range=ReviewWindow(start_frame=7, end_frame=7),
        )


def test_bundle_accepts_optional_video_evidence_compatibly() -> None:
    """T4 compat: AssessmentEvidenceBundle gains an OPTIONAL video clip;
    every existing frame-refs-only construction stays valid (default None)."""

    clip = _evidence(ref="file:///var/empty/t4/clip.mp4")
    with_video = AssessmentEvidenceBundle(episode_id=EPISODE_ID, window=WINDOW, video=clip)
    assert with_video.video == clip
    without = AssessmentEvidenceBundle(episode_id=EPISODE_ID, window=WINDOW)
    assert without.video is None


# ------------------------------------------------------------ local verification


def test_verify_local_clip_accepts_matching_bytes_and_probes_real_streams(
    pinned_tools: PinnedAudioTools, ntsc_av_source: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A truthful evidence object over REAL bytes verifies and returns the
    ACTUAL probed stream facts (never the declared flag alone)."""

    silent = _encode_silent(pinned_tools, tmp_path_factory.mktemp("t4-silent") / "silent.mp4")
    av_clip = _evidence(
        ref=ntsc_av_source.as_uri(), sha256=sha256_file(ntsc_av_source), audio_present=True
    )
    verified_av = verify_local_clip(av_clip)
    assert verified_av.path == ntsc_av_source
    assert verified_av.audio_streams == 1
    assert verified_av.has_audio

    silent_clip = _evidence(ref=silent.as_uri(), sha256=sha256_file(silent))
    verified_silent = verify_local_clip(silent_clip, audio_forbidden=True)
    assert verified_silent.audio_streams == 0
    assert not verified_silent.has_audio


def _encode_silent(pinned_tools: PinnedAudioTools, target: Path) -> Path:
    result = subprocess.run(
        (
            str(pinned_tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30000/1001:duration=1",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return target


def test_verify_local_clip_rejections_are_typed(
    pinned_tools: PinnedAudioTools, ntsc_av_source: Path, tmp_path: Path
) -> None:
    good = _evidence(ref=ntsc_av_source.as_uri(), sha256=sha256_file(ntsc_av_source))

    missing = good.model_copy(update={"sha256": "b" * 64, "ref": (tmp_path / "gone.mp4").as_uri()})
    with pytest.raises(VideoClipError, match="missing"):
        verify_local_clip(missing)

    stale_hash = good.model_copy(update={"sha256": "c" * 64})
    with pytest.raises(VideoClipError, match="hash"):
        verify_local_clip(stale_hash)

    directory = good.model_copy(update={"ref": tmp_path.as_uri()})
    with pytest.raises(VideoClipError, match="file"):
        verify_local_clip(directory)

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    empty_clip = _evidence(ref=empty.as_uri(), sha256=sha256_file(empty))
    with pytest.raises(VideoClipError, match="empty"):
        verify_local_clip(empty_clip)


def test_verify_local_clip_requires_declared_audio_to_match_actual_streams(
    ntsc_av_source: Path,
) -> None:
    """Root repro 1 seam: a forged audio_present flag (either direction) is a
    typed rejection even though the file and hash are perfectly real."""

    forged_silent = _evidence(
        ref=ntsc_av_source.as_uri(), sha256=sha256_file(ntsc_av_source), audio_present=False
    )
    with pytest.raises(VideoClipError, match="audio_present"):
        verify_local_clip(forged_silent)

    honest = _evidence(
        ref=ntsc_av_source.as_uri(), sha256=sha256_file(ntsc_av_source), audio_present=True
    )
    with pytest.raises(VideoClipError, match="audio stream"):
        verify_local_clip(honest, audio_forbidden=True)
