"""Static proof generator: two frames + theme text composites."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image
from pydantic import ValidationError

from services.cli.review_common import load_tools
from services.cli.v44_real01 import V44EpisodeProtocolV1
from services.foundation_io import sha256_file
from services.presentation.overlay_render import OverlayRenderError, extract_frame
from services.presentation.theme_text import ThemeTextError, ThemeVariant, render_theme_text

_W = 1920
_H = 1080
_FRAME_OPENING = 30
_FRAME_PERSISTENT = 120
_NAME_OPENING = "opening-theme-proof.png"
_NAME_PERSISTENT = "persistent-theme-proof.png"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class ThemeProofError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ThemeProofRecord:
    path: Path
    sha256: str
    video_sha256: str


class FrameExtractor(Protocol):
    def __call__(
        self, media: Path, *, frame_index: int, width: int, height: int, pix_fmt: str
    ) -> bytes: ...


class TextRenderer(Protocol):
    def __call__(
        self, text: str, *, variant: ThemeVariant, font_path: Path
    ) -> Image.Image: ...


@dataclass(frozen=True, slots=True)
class ThemeProofRequest:
    """Inputs for one proof generation: media paths plus the video pin."""

    video: Path
    protocol: Path
    font: Path
    output_dir: Path
    expected_video_sha256: str


def _require_file(path: Path, code: str) -> None:
    if not path.is_file():
        raise ThemeProofError(code, f"missing file: {path}")


def _load_title(protocol: Path) -> str:
    try:
        model = V44EpisodeProtocolV1.model_validate_json(protocol.read_bytes())
    except OSError as error:
        raise ThemeProofError("protocol-invalid", f"cannot read protocol: {error}") from error
    except ValidationError as error:
        raise ThemeProofError("protocol-invalid", f"protocol validation failed: {error}") from error
    title = model.title_intent
    if title is None or not str(title).strip():
        raise ThemeProofError("theme-title-missing", f"title_intent missing or blank in {protocol}")
    return str(title)


def _composite_frame(frame_bytes: bytes, overlay: Image.Image) -> Image.Image:
    base = Image.frombytes("RGB", (_W, _H), frame_bytes)
    base_rgba = base.convert("RGBA")
    if not isinstance(overlay, Image.Image):
        raise ThemeProofError("overlay-invalid", "overlay must be PIL Image")
    composited = Image.alpha_composite(base_rgba, overlay)
    return composited.convert("RGB")


def _pinned_extractor() -> FrameExtractor:
    tools = load_tools()
    ffmpeg_bin = tools.ffmpeg

    def _extract(
        media: Path, *, frame_index: int, width: int, height: int, pix_fmt: str
    ) -> bytes:
        return extract_frame(
            ffmpeg_bin,
            media,
            frame_index=frame_index,
            width=width,
            height=height,
            pix_fmt=pix_fmt,
        )

    return _extract


def _require_video_pin(request: ThemeProofRequest) -> str:
    _require_file(request.video, "video-not-found")
    _require_file(request.protocol, "protocol-not-found")
    _require_file(request.font, "font-not-found")
    if _SHA256_HEX.fullmatch(request.expected_video_sha256) is None:
        raise ThemeProofError(
            "video-sha256-invalid",
            "expected video sha256 must be 64 lowercase hex chars: "
            f"{request.expected_video_sha256!r}",
        )
    video_sha256 = sha256_file(request.video)
    if video_sha256 != request.expected_video_sha256:
        raise ThemeProofError(
            "video-sha256-mismatch",
            f"video sha256 {video_sha256} != expected {request.expected_video_sha256}",
        )
    return video_sha256


def _overlay_pair(
    title: str, font: Path, text_renderer: TextRenderer | None
) -> tuple[Image.Image, Image.Image]:
    if text_renderer is None:
        return (
            render_theme_text(title, variant="opening", font_path=font),
            render_theme_text(title, variant="persistent", font_path=font),
        )
    return (
        text_renderer(text=title, variant="opening", font_path=font),
        text_renderer(text=title, variant="persistent", font_path=font),
    )


def _save_composite(
    frame_bytes: bytes, overlay: Image.Image, destination: Path, video_sha256: str
) -> ThemeProofRecord:
    _composite_frame(frame_bytes, overlay).save(
        destination, format="PNG", compress_level=1, optimize=False
    )
    return ThemeProofRecord(
        path=destination, sha256=sha256_file(destination), video_sha256=video_sha256
    )


def generate_theme_proofs(
    request: ThemeProofRequest,
    *,
    frame_extractor: FrameExtractor | None = None,
    text_renderer: TextRenderer | None = None,
) -> tuple[ThemeProofRecord, ThemeProofRecord]:
    video_sha256 = _require_video_pin(request)
    title = _load_title(request.protocol)
    extractor = frame_extractor or _pinned_extractor()
    frame_opening = extractor(
        request.video, frame_index=_FRAME_OPENING, width=_W, height=_H, pix_fmt="rgb24"
    )
    frame_persistent = extractor(
        request.video, frame_index=_FRAME_PERSISTENT, width=_W, height=_H, pix_fmt="rgb24"
    )
    expected_len = _W * _H * 3
    if len(frame_opening) != expected_len or len(frame_persistent) != expected_len:
        raise ThemeProofError("frame-size-mismatch", f"expected {expected_len} bytes per frame")
    request.output_dir.mkdir(parents=True, exist_ok=True)
    overlay_opening, overlay_persistent = _overlay_pair(title, request.font, text_renderer)
    return (
        _save_composite(
            frame_opening, overlay_opening,
            request.output_dir / _NAME_OPENING, video_sha256,
        ),
        _save_composite(
            frame_persistent, overlay_persistent,
            request.output_dir / _NAME_PERSISTENT, video_sha256,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m services.cli.v44_theme_proof")
    p.add_argument("--video", required=True, type=Path, help="preview video path (30fps 1920x1080)")
    p.add_argument("--protocol", required=True, type=Path, help="v44 episode protocol JSON")
    p.add_argument("--font", required=True, type=Path, help="bold Japanese font file")
    p.add_argument("--output-dir", required=True, type=Path, help="output directory for two PNGs")
    p.add_argument(
        "--expected-video-sha256",
        required=True,
        help="expected sha256 of the source video (64 lowercase hex)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        records = generate_theme_proofs(
            ThemeProofRequest(
                video=Path(args.video),
                protocol=Path(args.protocol),
                font=Path(args.font),
                output_dir=Path(args.output_dir),
                expected_video_sha256=args.expected_video_sha256,
            )
        )
    except ThemeProofError as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 2
    except ThemeTextError as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 2
    except OverlayRenderError as error:
        print(f"overlay-render-error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"file-error: {error}", file=sys.stderr)
        return 2
    for rec in records:
        print(f"{rec.path.name} sha256={rec.sha256} video-sha256={rec.video_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
