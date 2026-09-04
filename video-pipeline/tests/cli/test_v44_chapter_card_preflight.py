"""Cheap gates run before any decode/subprocess, and their ordering (TDD)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import ImageFont

import services.cli._v44_chapter_card_gates as gates_module
import services.cli._v44_chapter_card_preflight as preflight
import services.cli._v44_chapter_card_run as run_module
from services.cli._v44_chapter_card_gates import ChapterCardInsertError
from services.cli._v44_chapter_card_plan import ChapterCardPlanError
from services.cli._v44_chapter_card_preflight import (
    require_approved_font,
    require_pins_current,
)
from services.cli.v44_chapter_card_insert import main
from services.foundation_io import sha256_file
from services.presentation.chapter_card import FONT_INDEX_W6
from services.preview.models import PreviewToolchainError
from services.preview.tools import PinnedTools
from tests.cli.v44_chapter_episode_workspace import (
    episode_workspace,
    fake_tools,
)
from tests.cli.v44_chapter_sidecar_factory import write_sidecar

APPROVED_FONT = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")


def _forbid_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any decode attempt before the cheap gates is an instant test failure."""

    def _explode(*args: object, **kwargs: object) -> bytes:
        message = "decode/subprocess reached before the cheap gates passed"
        raise AssertionError(message)

    monkeypatch.setattr(run_module, "decode_pcm_s16le", _explode)


def _workspace(tmp_path: Path, *, source_bytes: bytes) -> tuple[Path, Path, Path, Path]:
    episode_root, diag_root = episode_workspace(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    sidecar = write_sidecar(tmp_path)
    plan = episode_root / "review" / "store" / "plan-v3.json"
    return source, sidecar, plan, diag_root


def _fake_plan(path: Path, subtitle_items: int) -> None:
    items: list[dict[str, str]] = [{"kind": "video", "item_id": f"s{i}"} for i in range(4)]
    items.extend({"kind": "subtitle", "item_id": f"st{i}"} for i in range(subtitle_items))
    path.write_text(json.dumps({"plan": {"items": items}}), encoding="utf-8")


def test_run_insertion_refuses_source_drift_before_any_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a source render whose hash is not the approved one
    # When: running the insertion
    # Then: typed refusal and not a single decode/subprocess call
    source, sidecar, plan, diag = _workspace(tmp_path, source_bytes=b"not-the-source")
    _fake_plan(plan, 97)
    _forbid_decode(monkeypatch)
    tools = fake_tools(source, source)
    with pytest.raises(ChapterCardInsertError, match="source-hash-mismatch"):
        run_module.run_insertion(
            tools,
            run_module.InsertionRequest(
                source=source,
                proposal=sidecar,
                font=APPROVED_FONT,
                output_dir=tmp_path / "out",
                episode_root=tmp_path / "episodes" / "ep-457dfac97989568e",
                diag_finishing_root=diag,
                protected_before={},
                ),
            )


def test_run_insertion_refuses_pin_drift_before_any_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: pinned binaries whose on-disk hash no longer matches the lock
    # When: running the insertion
    # Then: typed toolchain refusal, zero decode/subprocess calls
    source, sidecar, plan, diag = _workspace(tmp_path, source_bytes=b"any")
    _fake_plan(plan, 97)
    _forbid_decode(monkeypatch)
    drifted = PinnedTools(
        ffmpeg=source, ffprobe=source, ffmpeg_sha256="0" * 64, ffprobe_sha256="0" * 64
    )
    episode_root = source.parents[0] / "episodes" / "ep-457dfac97989568e"
    with pytest.raises(ChapterCardInsertError, match="toolchain-pin-drift"):
        run_module.run_insertion(
            drifted,
            run_module.InsertionRequest(
                source=source,
                proposal=sidecar,
                font=APPROVED_FONT,
                output_dir=tmp_path / "out",
                episode_root=episode_root,
                diag_finishing_root=diag,
                protected_before={},
                ),
            )


def test_run_insertion_refuses_bad_proposal_before_any_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a valid source but a proposal missing the approved candidate
    # When: running the insertion
    # Then: typed sidecar refusal, zero decode/subprocess calls
    source, _, plan, diag = _workspace(tmp_path, source_bytes=b"any")
    _fake_plan(plan, 97)
    sidecar = write_sidecar(tmp_path, mutate="candidate_id", value="f" * 64)
    _forbid_decode(monkeypatch)
    monkeypatch.setattr(preflight, "SOURCE_VIDEO_SHA256", sha256_file(source))
    tools = fake_tools(source, source)
    episode_root = tmp_path / "episodes" / "ep-457dfac97989568e"
    with pytest.raises((ChapterCardPlanError, ChapterCardInsertError)):
        run_module.run_insertion(
            tools,
            run_module.InsertionRequest(
                source=source,
                proposal=sidecar,
                font=APPROVED_FONT,
                output_dir=tmp_path / "out",
                episode_root=episode_root,
                diag_finishing_root=diag,
                protected_before={},
                ),
            )


def test_run_insertion_refuses_plan_drift_before_any_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: valid source and proposal but a review plan that drifted
    # When: running the insertion
    # Then: typed plan refusal, zero decode/subprocess calls
    source, sidecar, plan, diag = _workspace(tmp_path, source_bytes=b"any")
    _fake_plan(plan, 97)  # 97 items but bytes differ from the approved plan hash
    _forbid_decode(monkeypatch)
    monkeypatch.setattr(preflight, "SOURCE_VIDEO_SHA256", sha256_file(source))
    tools = fake_tools(source, source)
    episode_root = tmp_path / "episodes" / "ep-457dfac97989568e"
    with pytest.raises(ChapterCardInsertError, match="plan-drift"):
        run_module.run_insertion(
            tools,
            run_module.InsertionRequest(
                source=source,
                proposal=sidecar,
                font=APPROVED_FONT,
                output_dir=tmp_path / "out",
                episode_root=episode_root,
                diag_finishing_root=diag,
                protected_before={},
                ),
            )


def test_run_insertion_refuses_wrong_font_before_any_decode_or_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: valid source/proposal/plan but a font that is not the approved file
    # When: running the insertion
    # Then: typed font refusal before any decode or card render
    source, sidecar, plan, diag = _workspace(tmp_path, source_bytes=b"any")
    _fake_plan(plan, 97)
    wrong_font = tmp_path / "wrong.ttc"
    wrong_font.write_bytes(b"loadable-font-bytes-are-not")
    _forbid_decode(monkeypatch)

    def _no_render(*args: object, **kwargs: object) -> None:
        message = "card render reached before the font gate passed"
        raise AssertionError(message)

    monkeypatch.setattr(run_module, "render_chapter_card", _no_render)
    monkeypatch.setattr(preflight, "SOURCE_VIDEO_SHA256", sha256_file(source))
    monkeypatch.setattr(gates_module, "BASE_PLAN_SHA256", sha256_file(plan))
    tools = fake_tools(source, source)
    episode_root = tmp_path / "episodes" / "ep-457dfac97989568e"
    with pytest.raises(ChapterCardInsertError, match="font-hash-drift"):
        run_module.run_insertion(
            tools,
            run_module.InsertionRequest(
                source=source,
                proposal=sidecar,
                font=wrong_font,
                output_dir=tmp_path / "out",
                episode_root=episode_root,
                diag_finishing_root=diag,
                protected_before={},
                ),
            )


def test_require_approved_font_measures_hash_and_face(tmp_path: Path) -> None:
    # Given: the approved system font copied to a fresh path
    # When: the font gate runs
    # Then: the measured hash and face come back and match the pin
    if not APPROVED_FONT.is_file():
        pytest.skip("approved font not installed")
    copy = tmp_path / "font.ttc"
    copy.write_bytes(APPROVED_FONT.read_bytes())
    facts = require_approved_font(copy)
    assert facts.sha256 == sha256_file(APPROVED_FONT)
    assert facts.face == ("Hiragino Sans GB", "W6")
    assert facts.path == copy


def test_require_approved_font_rejects_loadable_wrong_face(tmp_path: Path) -> None:
    # Given: the approved font file (hash pin satisfied) with the face pin
    #        rebound to a different face
    # When: the font gate runs
    # Then: typed face-drift refusal naming the measured face
    if not APPROVED_FONT.is_file():
        pytest.skip("approved font not installed")
    copy = tmp_path / "font.ttc"
    copy.write_bytes(APPROVED_FONT.read_bytes())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(preflight, "FONT_FACE", ("Hiragino Sans GB", "W3"))
    try:
        with pytest.raises(ChapterCardInsertError, match="font-face-drift"):
            preflight.require_approved_font(copy)
    finally:
        monkeypatch.undo()


def test_require_approved_font_checks_hash_before_loading(tmp_path: Path) -> None:
    # Given: unreadable bytes whose hash cannot match the pin
    # When: the font gate runs
    # Then: hash refusal before PIL ever loads the file
    bogus = tmp_path / "bogus.ttc"
    bogus.write_bytes(b"\x00\x01not-a-font")

    class _Poison:
        @staticmethod
        def truetype(*args: object, **kwargs: object) -> object:
            message = "font loaded before hash gate"
            raise AssertionError(message)

    patch = pytest.MonkeyPatch()
    patch.setattr(preflight, "ImageFont", _Poison())
    try:
        with pytest.raises(ChapterCardInsertError, match="font-hash-drift"):
            preflight.require_approved_font(bogus)
    finally:
        patch.undo()


def test_require_pins_current_translates_toolchain_error(tmp_path: Path) -> None:
    # Given: pinned tools that fail lock verification
    # When: the preflight pin gate runs
    # Then: the PreviewToolchainError becomes a typed insertion error
    marker = tmp_path / "bin"
    marker.write_bytes(b"x")
    drifted = PinnedTools(
        ffmpeg=marker, ffprobe=marker, ffmpeg_sha256="0" * 64, ffprobe_sha256="0" * 64
    )
    with pytest.raises(ChapterCardInsertError, match="toolchain-pin-drift"):
        require_pins_current(drifted)
    ok = fake_tools(marker, marker)
    require_pins_current(ok)  # exact-hash pins pass through


def test_font_index_w6_selects_the_bold_face() -> None:
    # Given: the approved font collection
    # When: loading face index 2 at render size
    # Then: the face is the pinned bold W6 (guards the index constant)
    if not APPROVED_FONT.is_file():
        pytest.skip("approved font not installed")
    face = ImageFont.truetype(str(APPROVED_FONT), 97, index=FONT_INDEX_W6).getname()
    assert (str(face[0]), str(face[1])) == ("Hiragino Sans GB", "W6")


def test_cli_translates_toolchain_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Given: a toolchain lock that cannot be verified
    # When: the CLI runs
    # Then: exit code 2 with a typed message, no traceback
    def _boom() -> PinnedTools:
        raise PreviewToolchainError("lock unreadable")

    monkeypatch.setattr("services.cli.v44_chapter_card_insert.load_tools", _boom)
    code = main(
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
    assert "toolchain" in stderr
    assert "Traceback" not in stderr
