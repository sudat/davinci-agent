from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.timeline_ir import TimelineIr0A
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut import build_base_cut
from services.resolve_bridge.base_cut_compare import (
    CODE_FRAME,
    CODE_ITEM_COUNT,
    CODE_MEDIA,
    CODE_TRACK,
    CODE_TRACK_COUNT,
    CompareOutcome,
    compare,
)
from services.resolve_bridge.base_cut_faults import build_fake_connection
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
FAULTS = Path("tests/fixtures/resolve-bridge-faults")
FIXTURE_DIR = Path("/nonexistent-fixture-dir-for-fakes")


def _run_fault(fault: str) -> tuple[CompareOutcome, list[str]]:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    media = fixture_media_map(FIXTURE_DIR)
    request = request_from_ir(ir_from_manifest(manifest), media, require_files=False)
    expected = expected_from_manifest(manifest, media)
    connection, manager = build_fake_connection(fault)
    snapshot = build_base_cut(connection, request)
    outcome = compare(expected, snapshot)
    leftover = [name for name in manager.GetProjectListInCurrentFolder() if name.startswith("__")]
    return outcome, leftover


def test_clean_fake_passes_with_zero_deltas() -> None:
    outcome, leftover = _run_fault("")
    assert outcome.passed
    assert outcome.max_source_delta == 0
    assert outcome.max_record_delta == 0
    assert outcome.media_all_match
    assert outcome.track_all_match
    assert outcome.link_all_match
    assert len(outcome.rows) == 8
    assert leftover == []


def test_wrong_media_same_duration_detected() -> None:
    outcome, leftover = _run_fault("wrong_media_same_duration")
    assert not outcome.passed
    assert CODE_MEDIA in {mismatch.code for mismatch in outcome.mismatches}
    assert not outcome.media_all_match
    assert leftover == []


def test_off_by_one_frame_detected() -> None:
    outcome, leftover = _run_fault("off_by_one_frame")
    assert not outcome.passed
    assert CODE_FRAME in {mismatch.code for mismatch in outcome.mismatches}
    assert outcome.max_record_delta == 1
    assert leftover == []


def test_wrong_track_detected() -> None:
    outcome, leftover = _run_fault("wrong_track")
    assert not outcome.passed
    assert CODE_TRACK in {mismatch.code for mismatch in outcome.mismatches}
    assert CODE_TRACK_COUNT in {mismatch.code for mismatch in outcome.mismatches}
    assert leftover == []


def test_duplicate_append_detected() -> None:
    outcome, leftover = _run_fault("duplicate_append")
    assert not outcome.passed
    assert CODE_ITEM_COUNT in {mismatch.code for mismatch in outcome.mismatches}
    assert leftover == []


@pytest.mark.parametrize(
    ("fixture", "marker"),
    [
        ("base-cut-wrong-media.json", "media-path-mismatch"),
        ("base-cut-off-by-one.json", "frame-mismatch"),
        ("base-cut-wrong-track.json", "track-mismatch"),
        ("base-cut-duplicate-append.json", "item-count-mismatch"),
    ],
)
def test_cli_fault_mode_detects_and_cleans_up(fixture: str, marker: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.resolve_bridge.base_cut",
            "--manifest",
            str(MANIFEST),
            "--fixture-dir",
            str(FIXTURE_DIR),
        ],
        env=os.environ | {"QA_FAULT_FIXTURE": str(FAULTS / fixture)},
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert marker in combined
    assert "owned projects leaked" not in combined


def test_malformed_ir_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TimelineIr0A.model_validate({})
    with pytest.raises(ValidationError):
        TimelineIr0A.model_validate({"rate": {"num": 0, "den": 1}, "tracks": []})


def test_ir_rejects_item_kind_mismatched_with_track() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    ir = ir_from_manifest(manifest)
    audio_track = ir.tracks[1]
    swapped = audio_track.items[0].model_copy(update={"kind": "video"})
    mixed = audio_track.model_copy(update={"items": (swapped, *audio_track.items[1:])})
    with pytest.raises(ValidationError):
        TimelineIr0A.model_validate(
            ir.model_copy(update={"tracks": (ir.tracks[0], mixed)}).model_dump()
        )


def test_missing_media_source_rejected() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    ir = ir_from_manifest(manifest)
    with pytest.raises(BaseCutError, match="media map is missing source"):
        request_from_ir(ir, {"unexpected": "/nonexistent/x.mov"}, require_files=False)


def test_unpaired_av_link_rejected() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    ir = ir_from_manifest(manifest)
    audio_track = ir.tracks[1]
    orphaned = audio_track.model_copy(update={"items": audio_track.items[1:]})
    broken = ir.model_copy(update={"tracks": (ir.tracks[0], orphaned)})
    with pytest.raises(BaseCutError, match="must pair exactly one video and one audio item"):
        request_from_ir(broken, fixture_media_map(FIXTURE_DIR), require_files=False)
