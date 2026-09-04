"""Cheap pre-decode gates: pinned tools, source identity, approved font."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import ImageFont

from services.cli._v44_chapter_card_gates import ChapterCardInsertError
from services.cli._v44_chapter_card_plan import FONT_FACE, FONT_SHA256, SOURCE_VIDEO_SHA256
from services.foundation_io import sha256_file
from services.presentation.chapter_card import FONT_INDEX_W6
from services.preview.models import PreviewToolchainError

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools


@dataclass(frozen=True, slots=True)
class FontFacts:
    path: Path
    sha256: str
    face: tuple[str, str]


def require_pins_current(tools: PinnedTools) -> None:
    """Re-hash both pinned binaries against the frozen lock before anything else."""

    try:
        tools.verify_current()
    except PreviewToolchainError as error:
        raise ChapterCardInsertError("toolchain-pin-drift", str(error)) from error


def require_source_video_intact(source: Path) -> None:
    if not source.is_file():
        raise ChapterCardInsertError("source-missing", f"approved source render absent: {source}")
    try:
        digest = sha256_file(source)
    except OSError as error:
        raise ChapterCardInsertError(
            "source-unreadable", f"cannot read source render {source}: {error}"
        ) from error
    if digest != SOURCE_VIDEO_SHA256:
        raise ChapterCardInsertError("source-hash-mismatch", f"source render drifted: {source}")


def require_approved_font(font: Path) -> FontFacts:
    """The exact approved font file, by hash and by measured face, before any decode."""

    if not font.is_file():
        raise ChapterCardInsertError("font-missing", f"approved font absent: {font}")
    try:
        measured_sha = sha256_file(font)
    except OSError as error:
        raise ChapterCardInsertError(
            "font-unreadable", f"cannot read font {font}: {error}"
        ) from error
    if measured_sha != FONT_SHA256:
        raise ChapterCardInsertError(
            "font-hash-drift", f"font {font} hashes {measured_sha}, not the approved file"
        )
    try:
        face = ImageFont.truetype(str(font), 16, index=FONT_INDEX_W6).getname()
    except OSError as error:
        raise ChapterCardInsertError(
            "font-unreadable", f"cannot load font {font}: {error}"
        ) from error
    measured_face = (str(face[0]), str(face[1]))
    if measured_face != FONT_FACE:
        raise ChapterCardInsertError(
            "font-face-drift",
            f"font face is {measured_face}, expected the approved {FONT_FACE}",
        )
    return FontFacts(path=font, sha256=measured_sha, face=measured_face)


__all__ = [
    "FontFacts",
    "require_approved_font",
    "require_pins_current",
    "require_source_video_intact",
]
