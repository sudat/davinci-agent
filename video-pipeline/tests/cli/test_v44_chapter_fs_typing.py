"""Typed OSError at every chapter-card filesystem boundary (call-site faults)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import services.cli._v44_chapter_card_gates as gates_module
import services.cli._v44_chapter_card_preflight as preflight
import services.cli._v44_chapter_card_qa as qa_module
import services.cli.v44_chapter_card_insert as cli
from services.cli._v44_chapter_card_gates import ChapterCardInsertError, require_plan_v3
from services.cli._v44_chapter_card_media import ChapterCardMediaError
from services.cli._v44_chapter_card_render import write_card_raw
from tests.cli.v44_chapter_scaled_episode import install_world, make_scaled_world, run_world_once
from tests.cli.v44_chapter_sidecar_factory import write_sidecar

APPROVED_FONT = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")


def _permits_denied(monkeypatch: pytest.MonkeyPatch, target_module: object, name: str) -> None:
    def _denied(path: Path) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(target_module, name, _denied)


def test_preflight_source_hash_read_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an approved source whose bytes cannot be read
    # When: the preflight source gate hashes it
    # Then: typed source-unreadable refusal, not a raw PermissionError
    source = tmp_path / "source.mp4"
    source.write_bytes(b"render")
    monkeypatch.setattr(preflight, "SOURCE_VIDEO_SHA256", "0" * 64)
    _permits_denied(monkeypatch, preflight, "sha256_file")
    with pytest.raises(ChapterCardInsertError, match="source-unreadable"):
        preflight.require_source_video_intact(source)


def test_preflight_font_hash_read_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a font file whose bytes cannot be read
    # When: the preflight font gate hashes it
    # Then: typed font-unreadable refusal before any PIL load
    font = tmp_path / "font.ttc"
    font.write_bytes(b"font")
    _permits_denied(monkeypatch, preflight, "sha256_file")

    class _Poison:
        @staticmethod
        def truetype(*args: object, **kwargs: object) -> object:
            raise AssertionError("font loaded before the hash read gate")

    monkeypatch.setattr(preflight, "ImageFont", _Poison())
    with pytest.raises(ChapterCardInsertError, match="font-unreadable"):
        preflight.require_approved_font(font)


def test_gates_plan_hash_read_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a review plan whose bytes cannot be read after parsing succeeded
    # When: the plan gate hashes it
    # Then: typed plan-unreadable refusal
    plan = tmp_path / "plan-v3.json"
    plan.write_text('{"plan": {"items": []}}', encoding="utf-8")
    _permits_denied(monkeypatch, gates_module, "sha256_file")
    with pytest.raises(ChapterCardInsertError, match="plan-unreadable"):
        require_plan_v3(plan)


def test_qa_raw_span_open_failure_is_typed(tmp_path: Path) -> None:
    # Given: a raw span dump with no read permission
    # When: the card-span gate iterates its frames
    # Then: typed QA refusal wrapping the OSError
    raw = tmp_path / "span.raw"
    raw.write_bytes(b"\x00" * 64)
    raw.chmod(0o000)
    try:
        with pytest.raises(qa_module.ChapterCardQAError, match="qa-file-failed"):
            list(qa_module._iter_raw_frames(raw, count=1))
    finally:
        raw.chmod(0o600)


def test_render_card_raw_write_failure_is_typed(tmp_path: Path) -> None:
    # Given: a work directory without write permission
    # When: writing the repeated card frame image
    # Then: typed media refusal naming the card file
    work = tmp_path / "work"
    work.mkdir()
    work.chmod(0o500)
    try:
        with pytest.raises(ChapterCardMediaError, match="card-write-failed"):
            write_card_raw(
                Image.new("RGB", (4, 4)), frames=2, path=work / "card.rgb"
            )
    finally:
        work.chmod(0o700)


def test_cli_final_master_hash_failure_is_typed_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Given: a completed scaled run whose published master cannot be re-hashed
    # When: the CLI finishes
    # Then: exit 2 with a typed fs-stat-failed message, no traceback
    world = make_scaled_world(tmp_path / "world")
    _, _, output_dir = run_world_once(tmp_path, world, monkeypatch)
    install_world(monkeypatch, world)
    tools, episode_root, diag_root, source = world
    sidecar = write_sidecar(source.parent, scaled=True)

    def _denied(path: Path) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "sha256_file", _denied)
    monkeypatch.setattr(
        "services.cli.v44_chapter_card_insert.load_tools", lambda: tools
    )
    code = cli.main(
        [
            "--source", str(source),
            "--proposal", str(sidecar),
            "--font", str(APPROVED_FONT),
            "--output-dir", str(output_dir),
            "--episode-root", str(episode_root),
            "--diag-finishing-root", str(diag_root),
        ]
    )
    assert code == 2
    stderr = capsys.readouterr().err
    assert "fs-stat-failed" in stderr
    assert "Traceback" not in stderr


def test_cli_last_resort_oserror_is_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # Given: an OSError escaping every typed boundary
    # When: the CLI runs
    # Then: last-resort typed fs-failure message and exit 2, no traceback
    def _boom(*args: object, **kwargs: object) -> tuple[Path, ...]:
        raise IsADirectoryError(21, "Is a directory")

    monkeypatch.setattr("services.cli.v44_chapter_card_insert.protected_paths", _boom)
    code = cli.main(
        [
            "--source", str(tmp_path / "s.mp4"),
            "--proposal", str(tmp_path / "p.json"),
            "--font", str(tmp_path / "f.ttc"),
            "--output-dir", str(tmp_path / "o"),
            "--episode-root", str(tmp_path / "e"),
            "--diag-finishing-root", str(tmp_path / "d"),
        ]
    )
    assert code == 2
    stderr = capsys.readouterr().err
    assert "fs-failure" in stderr
    assert "Traceback" not in stderr


def test_iter_raw_frames_short_read_is_typed(tmp_path: Path) -> None:
    # Given: a raw span file shorter than one frame
    # When: iterating its frames
    # Then: typed card-extract-failed refusal
    short = tmp_path / "short.raw"
    short.write_bytes(b"\x00" * 8)
    with pytest.raises(qa_module.ChapterCardQAError, match="card-extract-failed"):
        list(qa_module._iter_raw_frames(short, count=1))
