"""v44 theme proof: source-video SHA-256 gate (TDD, Given/When/Then)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from services.cli import v44_theme_proof as theme_module
from services.cli.v44_theme_proof import (
    ThemeProofError,
    ThemeProofRecord,
    ThemeProofRequest,
    generate_theme_proofs,
)
from tests.cli.test_v44_theme_proof import (
    THEME,
    _dummy_font,
    _dummy_video,
    _ExtractSpy,
    _protocol_json,
    _render_stub,
)


def _generate(
    request: ThemeProofRequest, *, spy: _ExtractSpy
) -> tuple[ThemeProofRecord, ThemeProofRecord]:
    return generate_theme_proofs(
        request, frame_extractor=spy, text_renderer=_render_stub
    )


def test_sha256_mismatch_blocks_extraction_and_output(tmp_path: Path) -> None:
    # Given: expected hash that does not match the actual video bytes
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    spy = _ExtractSpy()
    wrong = "0" * 64

    # When/Then: typed video-sha256-mismatch, no extraction, no output dir
    with pytest.raises(ThemeProofError) as exc:
        _generate(
            ThemeProofRequest(
                video=video, protocol=protocol, font=font,
                output_dir=out, expected_video_sha256=wrong,
            ),
            spy=spy,
        )
    assert exc.value.code == "video-sha256-mismatch"
    assert spy.calls == []
    assert not out.exists()


def test_expected_sha256_format_enforced(tmp_path: Path) -> None:
    # Given: expected hashes that are not exactly 64 lowercase hex chars
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    for bad in ("", "abc", "A" * 64, ("0" * 63) + "g", "0" * 65):
        out = tmp_path / f"out-{abs(hash(bad))}"
        spy = _ExtractSpy()

        # When/Then: typed video-sha256-invalid, no extraction, no output dir
        with pytest.raises(ThemeProofError) as exc:
            _generate(
                ThemeProofRequest(
                    video=video, protocol=protocol, font=font,
                    output_dir=out, expected_video_sha256=bad,
                ),
                spy=spy,
            )
        assert exc.value.code == "video-sha256-invalid"
        assert spy.calls == []
        assert not out.exists()


def test_records_and_cli_output_include_actual_video_sha256(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the correct expected hash for the video bytes
    protocol = _protocol_json(tmp_path, THEME)
    video = _dummy_video(tmp_path)
    font = _dummy_font(tmp_path)
    out = tmp_path / "out"
    expected = hashlib.sha256(video.read_bytes()).hexdigest()
    spy = _ExtractSpy()

    # When: generating with the matching expected hash
    records = _generate(
        ThemeProofRequest(
            video=video, protocol=protocol, font=font,
            output_dir=out, expected_video_sha256=expected,
        ),
        spy=spy,
    )

    # Then: every record carries the exact source-video sha256
    assert records[0].video_sha256 == expected
    assert records[1].video_sha256 == expected

    # And: CLI output includes the same source-video sha256
    monkeypatch.setattr(
        "services.cli.v44_theme_proof.generate_theme_proofs", lambda *a, **k: records
    )
    rc = theme_module.main(
        [
            "--video", str(video),
            "--protocol", str(protocol),
            "--font", str(font),
            "--output-dir", str(out),
            "--expected-video-sha256", expected,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert expected in captured.out
