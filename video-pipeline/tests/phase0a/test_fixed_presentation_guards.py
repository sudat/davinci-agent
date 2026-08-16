from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut_plan import (
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.fixed_presentation import shifted_expected, shifted_request
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
    RenderEvidence,
)
from services.resolve_bridge.fixed_presentation_render import compare_render
from services.resolve_bridge.fixed_presentation_srt import (
    SubtitleTextError,
    cue_from_recipe,
    load_fixed_cue,
    parse_srt,
)

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
FAULTS = Path("tests/fixtures/resolve-bridge-faults")
FIXTURE_DIR = Path("/nonexistent-fixture-dir-for-fakes")

CANONICAL_SRT = "1\n00:00:06,000 --> 00:00:08,000\nPHASE 0A FIXED SUBTITLE\n"


def _manifest() -> Phase0AFixtureManifest:
    return Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())


def test_parse_srt_rejects_invalid_utf8() -> None:
    with pytest.raises(SubtitleTextError, match="not valid UTF-8"):
        parse_srt(b"\xff\xfe\x00bad")


def test_parse_srt_rejects_missing_timing() -> None:
    with pytest.raises(SubtitleTextError, match="without timing line"):
        parse_srt(b"1\nno timing here\nTEXT\n")


def test_parse_srt_reads_fixed_cue() -> None:
    (cue,) = parse_srt(CANONICAL_SRT.encode())
    assert (cue.start_ms, cue.end_ms, cue.text) == (6000, 8000, "PHASE 0A FIXED SUBTITLE")


def test_cue_from_recipe_uses_record_span_and_rate() -> None:
    manifest = _manifest()
    cue = cue_from_recipe(manifest.recipe.subtitle, 30, 1)
    assert cue.start_ms == 6000
    assert cue.end_ms == 8000
    assert cue.text == manifest.recipe.subtitle.text


def test_load_fixed_cue_accepts_canonical_srt(tmp_path: Path) -> None:
    manifest = _manifest()
    srt = tmp_path / "subtitle.srt"
    srt.write_text(CANONICAL_SRT)
    cue = load_fixed_cue(srt, manifest.recipe.subtitle, 30, 1)
    assert cue.start_ms == 6000
    assert cue.end_ms == 8000


def test_load_fixed_cue_rejects_wrong_text(tmp_path: Path) -> None:
    manifest = _manifest()
    srt = tmp_path / "subtitle.srt"
    srt.write_text("1\n00:00:06,000 --> 00:00:08,000\nWRONG TEXT\n")
    with pytest.raises(SubtitleTextError, match="!="):
        load_fixed_cue(srt, manifest.recipe.subtitle, 30, 1)


def test_load_fixed_cue_rejects_extra_cues(tmp_path: Path) -> None:
    manifest = _manifest()
    srt = tmp_path / "subtitle.srt"
    srt.write_text(CANONICAL_SRT + "\n2\n00:00:09,000 --> 00:00:10,000\nEXTRA\n")
    with pytest.raises(SubtitleTextError, match="exactly one cue"):
        load_fixed_cue(srt, manifest.recipe.subtitle, 30, 1)


def test_shift_preserves_relative_spans() -> None:
    manifest = _manifest()
    media = fixture_media_map(FIXTURE_DIR)
    request = request_from_ir(ir_from_manifest(manifest), media, require_files=False)
    expected = expected_from_manifest(manifest, media)
    shifted_req = shifted_request(request, 108000)
    shifted_exp = shifted_expected(expected, 108000)
    for original, moved in zip(request.items, shifted_req.items, strict=True):
        assert moved.record_start == original.record_start + 108000
        assert moved.record_end == original.record_end + 108000
        assert moved.source_start == original.source_start
        assert moved.source_end == original.source_end
    for original, moved in zip(expected.items, shifted_exp.items, strict=True):
        assert moved.record_end - moved.record_start == original.record_end - original.record_start


def _evidence(audio_channels: int, audio_codec: str, container: str) -> RenderEvidence:
    video = FfprobeStream(
        codec_type="video",
        codec_name="h264",
        width=1920,
        height=1080,
        pix_fmt="yuv420p",
        r_frame_rate="30/1",
        avg_frame_rate="30/1",
        nb_frames="660",
        duration="22.000000",
    )
    audio = FfprobeStream(
        codec_type="audio",
        codec_name=audio_codec,
        sample_rate="48000",
        channels=audio_channels,
        duration="22.080000",
    )
    return RenderEvidence(
        job_id="job",
        status="complete",
        output_path="/virtual/out.mp4",
        output_sha256="0" * 64,
        report=FfprobeReport(
            streams=(video, audio),
            format=FfprobeFormat(format_name="mov,mp4,m4a,3gp,3g2,mj2", duration=container),
        ),
        marks_in=108000,
        marks_out=108659,
    )


def test_compare_render_passes_frozen_expectations() -> None:
    summary, mismatches = compare_render(_evidence(2, "aac", "22.080000"), _manifest())
    assert mismatches == ()
    assert "frames=660" in summary


def test_compare_render_flags_audio_preset_mismatch() -> None:
    _, mismatches = compare_render(_evidence(1, "pcm_s16le", "22.080000"), _manifest())
    codes = {mismatch.code for mismatch in mismatches}
    assert "audio-preset-mismatch" in codes
    assert not any(mismatch.code == "render-video-mismatch" for mismatch in mismatches)


def test_compare_render_flags_container_duration_outside_bound() -> None:
    _, mismatches = compare_render(_evidence(2, "aac", "23.500000"), _manifest())
    codes = {mismatch.code for mismatch in mismatches}
    assert "render-format-mismatch" in codes


def test_compare_render_flags_frame_count_drift() -> None:
    evidence = _evidence(2, "aac", "22.080000")
    video, audio = evidence.report.streams
    drifted = video.model_copy(update={"nb_frames": "659"})
    report = evidence.model_copy(
        update={
            "report": FfprobeReport(
                streams=(drifted, audio),
                format=FfprobeFormat(
                    format_name="mov,mp4,m4a,3gp,3g2,mj2",
                    duration="22.080000",
                ),
            )
        }
    )
    _, mismatches = compare_render(report, _manifest())
    assert any("nb_frames" in mismatch.detail for mismatch in mismatches)


@pytest.mark.parametrize(
    ("fixture", "marker", "overall_pass_allowed"),
    [
        ("fixed-presentation-text-encoding.json", "code=text-encoding-unsupported", False),
        (
            "fixed-presentation-unsupported-track-placement.json",
            "placement=in-timeline:false",
            True,
        ),
        ("fixed-presentation-preset-mismatch.json", "code=audio-preset-mismatch", False),
        ("fixed-presentation-missing-fallback.json", "code=subtitle-placement-unsupported", False),
    ],
)
def test_cli_fault_mode_detects_and_cleans_up(
    fixture: str, marker: str, *, overall_pass_allowed: bool
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.resolve_bridge.fixed_presentation",
            "--manifest",
            str(MANIFEST),
            "--fixture-dir",
            "/nonexistent-fixture-dir-for-fakes",
        ],
        env=os.environ | {"QA_FAULT_FIXTURE": str(FAULTS / fixture)},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert marker in combined, combined
    assert "owned projects leaked" not in combined
    if not overall_pass_allowed:
        assert "fixed-presentation: PASS" not in combined
    else:
        assert "rung=direct available=false" in combined
        assert "strategy=external" in combined


def test_fault_fixtures_never_silently_pass() -> None:
    for name in (
        "fixed-presentation-text-encoding.json",
        "fixed-presentation-unsupported-track-placement.json",
        "fixed-presentation-preset-mismatch.json",
        "fixed-presentation-missing-fallback.json",
    ):
        spec = (FAULTS / name).read_text()
        assert "fault" in spec, name
