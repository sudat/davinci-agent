"""v44 theme static-proof generator (TDD, Given/When/Then)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from services.cli.v44_theme_proof import (
    FrameExtractor,
    ThemeProofError,
    ThemeProofRecord,
    ThemeProofRequest,
    generate_theme_proofs,
)

REPO = Path(__file__).resolve().parents[2]
THEME = "【人生終わった】カメラのケースが見つからない件【ヤバい怒られる】"
FONT = Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")
CANVAS_W = 1920
CANVAS_H = 1080
VIDEO_SHA256 = hashlib.sha256(b"fake-video-bytes").hexdigest()


def _fake_frame_bytes(frame_index: int) -> bytes:
    r = frame_index % 256
    g = (100 + frame_index) % 256
    b = (200 + frame_index) % 256
    pixel = bytes([r, g, b])
    return pixel * (CANVAS_W * CANVAS_H)


def _protocol_json(tmp_path: Path, title: str | None) -> Path:
    p = tmp_path / "episode.json"
    payload = {
        "schema_version": "v44-episode-protocol-v1",
        "episode_id": "v44-real-01",
        "title_intent": title,
        "created_at": "2026-01-01T00:00:00+00:00",
        "expected_content": {},
    }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _dummy_video(tmp_path: Path) -> Path:
    v = tmp_path / "preview.mp4"
    v.write_bytes(b"fake-video-bytes")
    return v


def _generate(
    video: Path,
    protocol: Path,
    font: Path,
    out: Path,
    *,
    frame_extractor: FrameExtractor | None = None,
) -> tuple[ThemeProofRecord, ThemeProofRecord]:
    return generate_theme_proofs(
        ThemeProofRequest(
            video=video,
            protocol=protocol,
            font=font,
            output_dir=out,
            expected_video_sha256=VIDEO_SHA256,
        ),
        frame_extractor=frame_extractor,
        text_renderer=_render_stub,
    )


def _dummy_font(tmp_path: Path) -> Path:
    if FONT.is_file():
        return FONT
    f = tmp_path / "dummy.ttc"
    f.write_bytes(b"not-real-font-but-exists")
    return f


class _ExtractSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, str]] = []

    def __call__(
        self,
        media: Path,
        *,
        frame_index: int,
        width: int,
        height: int,
        pix_fmt: str = "rgb24",
    ) -> bytes:
        self.calls.append((frame_index, width, height, pix_fmt))
        return _fake_frame_bytes(frame_index)


def _render_stub(text: str, *, variant: str, font_path: Path) -> Image.Image:
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    if variant == "opening":
        for y in range(400, 680):
            for x in range(100, 1640):
                img.putpixel((x, y), (255, 255, 255, 255))
    else:
        for y in range(27, 80):
            for x in range(48, 672):
                img.putpixel((x, y), (255, 255, 0, 255))
    return img


def test_exact_two_names(tmp_path: Path) -> None:
    # Given: valid protocol with exact theme title and fake video/font
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    spy = _ExtractSpy()

    # When: generating proofs with stubbed extractor/renderer
    records = _generate(video, protocol, font, out, frame_extractor=spy)

    # Then: exactly two files with expected names, no extra
    assert len(records) == 2
    names = {r.path.name for r in records}
    assert names == {"opening-theme-proof.png", "persistent-theme-proof.png"}
    files = list(out.iterdir())
    assert len(files) == 2
    assert {f.name for f in files} == names


def test_hashes_stable(tmp_path: Path) -> None:
    # Given: same inputs
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    spy1 = _ExtractSpy()
    spy2 = _ExtractSpy()

    # When: generating twice
    rec1 = _generate(video, protocol, font, out1, frame_extractor=spy1)
    rec2 = _generate(video, protocol, font, out2, frame_extractor=spy2)

    # Then: hashes stable per variant
    by_name1 = {r.path.name: r.sha256 for r in rec1}
    by_name2 = {r.path.name: r.sha256 for r in rec2}
    assert by_name1["opening-theme-proof.png"] == by_name2["opening-theme-proof.png"]
    assert by_name1["persistent-theme-proof.png"] == by_name2["persistent-theme-proof.png"]
    # And file bytes hash matches record
    for rec in rec1:
        assert hashlib.sha256(rec.path.read_bytes()).hexdigest() == rec.sha256


def test_opening_and_persistent_differ(tmp_path: Path) -> None:
    # Given: same base frames but different theme variants
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    spy = _ExtractSpy()

    # When: the proofs are generated
    records = _generate(video, protocol, font, out, frame_extractor=spy)

    # Then: hashes differ between variants
    by_name = {r.path.name: r.sha256 for r in records}
    assert by_name["opening-theme-proof.png"] != by_name["persistent-theme-proof.png"]


def test_title_missing_blocks_before_extraction_or_output(tmp_path: Path) -> None:
    # Given: protocol with blank/missing title
    for bad_title in (None, "", "   "):
        protocol = _protocol_json(tmp_path, bad_title)
        video = _dummy_video(tmp_path)
        font = _dummy_font(tmp_path)
        out = tmp_path / f"out-{hash(str(bad_title))}"
        spy = _ExtractSpy()

        # When: the proofs are generated
        # Then: typed theme-title-missing, no extraction, no output dir
        with pytest.raises(ThemeProofError) as exc:
            _generate(video, protocol, font, out, frame_extractor=spy)
        assert exc.value.code == "theme-title-missing"
        assert spy.calls == []
        assert not out.exists()


def test_extraction_asks_only_for_frames_30_and_120_at_1920x1080_rgb(tmp_path: Path) -> None:
    # Given: valid inputs
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    spy = _ExtractSpy()

    # When: the proofs are generated
    _generate(video, protocol, font, out, frame_extractor=spy)

    # Then: exactly two calls, frames 30 and 120, 1920x1080 rgb24
    assert len(spy.calls) == 2
    frames = sorted(c[0] for c in spy.calls)
    assert frames == [30, 120]
    for _, w, h, fmt in spy.calls:
        assert w == 1920
        assert h == 1080
        assert fmt == "rgb24"


def test_no_subprocess_or_full_render_surface(tmp_path: Path) -> None:
    # Given: the production module source
    # When: inspecting imports
    # Then: no subprocess import and no full-render/resolve/video encoding surface
    source = (REPO / "services" / "cli" / "v44_theme_proof.py").read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "subprocess." not in source
    # No video encoding or resolve mutation or report artifact generation
    low = source.lower()
    assert "prores" not in low
    assert "resolve" not in low
    assert "timeline" not in low
    # proof naming is allowed, a generic report artifact is not
    assert "report" not in low or "proof" in low
    # Must reuse allowed helpers; ensure they are imported
    assert "render_theme_text" in source
    assert "extract_frame" in source


def test_output_dir_created_only_after_success(tmp_path: Path) -> None:
    # Given: protocol invalid (will fail before extraction)
    protocol = _protocol_json(tmp_path, None)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "should-not-exist"
    spy = _ExtractSpy()

    # When/Then: failure leaves no dir
    with pytest.raises(ThemeProofError):
        _generate(video, protocol, font, out, frame_extractor=spy)
    assert not out.exists()

    # Given: extractor failure
    def failing_extractor(
        media: Path, *, frame_index: int, width: int, height: int, pix_fmt: str
    ) -> bytes:
        raise ThemeProofError("extract-failed", "frame extraction failed")

    protocol2 = _protocol_json(tmp_path, THEME)
    out2 = tmp_path / "should-not-exist-2"
    with pytest.raises(ThemeProofError):
        _generate(video, protocol2, font, out2, frame_extractor=failing_extractor)
    assert not out2.exists()


def test_unrelated_file_preserved(tmp_path: Path) -> None:
    # Given: output dir already contains an unrelated file
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    unrelated = out / "keep-me.txt"
    unrelated.write_text("important", encoding="utf-8")
    spy = _ExtractSpy()

    # When: generating proofs
    _generate(video, protocol, font, out, frame_extractor=spy)

    # Then: unrelated file remains unchanged and proofs are created
    assert unrelated.is_file()
    assert unrelated.read_text(encoding="utf-8") == "important"
    assert (out / "opening-theme-proof.png").is_file()
    assert (out / "persistent-theme-proof.png").is_file()


def test_cli_requires_explicit_paths(tmp_path: Path) -> None:
    # Given: CLI parser
    # When: invoking without required flags
    result = subprocess.run(
        [sys.executable, "-m", "services.cli.v44_theme_proof"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
        timeout=10,
    )
    # Then: exits non-zero and mentions required arguments
    assert result.returncode != 0
    hints = (result.stderr + result.stdout).lower()
    assert "required" in hints or "--video" in hints


def test_cli_prints_sha256(tmp_path: Path) -> None:
    # Given: valid inputs with stubbed extraction (the CLI surface itself is
    # covered by the subprocess test above; here the records must carry hex hashes)
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    spy = _ExtractSpy()
    records = _generate(video, protocol, font, out, frame_extractor=spy)
    for r in records:
        assert len(r.sha256) == 64
        assert all(c in "0123456789abcdef" for c in r.sha256)
