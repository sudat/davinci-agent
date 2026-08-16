from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.build_report import BuildReport0A, ItemPlacement0A
from services.contracts.serialization import artifact_content_hash
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.build_report_fakes import FakeSpike, run_fake_spike
from services.resolve_bridge.build_report_fingerprint import (
    fingerprint_bytes,
    timeline_fingerprint,
)
from services.resolve_bridge.build_report_verify import (
    CODE_ITEM_COUNT,
    CODE_ITEM_OBSERVED,
    CODE_RENDER_DECODE,
    CODE_RENDER_HASH,
    verify_report,
)
from services.resolve_bridge.fixed_presentation_fakes import FAKE_RENDER_MAGIC

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
FIXTURE_DIR = Path("/nonexistent-fixture-dir-for-fakes")
FAULTS = Path("tests/fixtures/resolve-bridge-faults")


def _manifest() -> Phase0AFixtureManifest:
    return Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())


@pytest.fixture
def spike(tmp_path: Path) -> FakeSpike:
    return run_fake_spike(MANIFEST, FIXTURE_DIR, tmp_path)


def _verify(current: FakeSpike, report: BuildReport0A):
    return verify_report(
        report,
        _manifest(),
        FIXTURE_DIR,
        current.tools,
        manifest_path=current.manifest_path,
        host_report_path=current.host_report_path,
        ffmpeg_bin=current.ffmpeg_bin,
        ffprobe_bin=current.ffprobe_bin,
    )


def _placement(item_id: str, kind: str, record: int, media: str) -> ItemPlacement0A:
    return ItemPlacement0A.model_validate(
        {
            "item_id": item_id,
            "kind": kind,
            "source": {
                "source_id": "source",
                "span": {"start_frame": 0, "end_frame": 30, "rate": {"num": 30, "den": 1}},
            },
            "record_span": {"start_frame": record, "end_frame": record + 30},
            "track": {"kind": kind, "index": 1},
            "av_link_id": "av-cut-001",
            "media_path": media,
        }
    )


def _placements() -> tuple[ItemPlacement0A, ...]:
    return (
        _placement("cut-001", "video", 30, "/m/source.mov"),
        _placement("cut-001", "audio", 30, "/m/source.mov"),
        _placement("cut-002", "video", 330, "/m/source.mov"),
        _placement("cut-002", "audio", 330, "/m/source.mov"),
    )


def test_fingerprint_is_deterministic_and_order_independent() -> None:
    baseline = timeline_fingerprint(_placements())
    assert timeline_fingerprint(tuple(reversed(_placements()))) == baseline


def test_fingerprint_changes_when_any_field_tampered() -> None:
    baseline = timeline_fingerprint(_placements())
    tampered = _placement("cut-001", "video", 31, "/m/source.mov")
    assert timeline_fingerprint((tampered, *_placements()[1:])) != baseline
    other_media = _placement("cut-001", "audio", 30, "/m/other.mov")
    assert timeline_fingerprint((*_placements()[:3], other_media)) != baseline


def test_fingerprint_canonical_bytes_are_pinned() -> None:
    assert fingerprint_bytes(_placements()[:1]) == (
        b"cut-001|video|source|0|30|30/1|30|60|video|1|av-cut-001|/m/source.mov\n"
    )


def test_content_hash_follows_genesis_convention(spike: FakeSpike) -> None:
    report = spike.report
    assert artifact_content_hash(report) == report.content_hash
    tampered = report.model_copy(
        update={"timeline_fingerprint": "0" * 64},
    )
    assert artifact_content_hash(tampered) != report.content_hash


def test_genuine_fake_report_verifies_green(spike: FakeSpike) -> None:
    assert len(spike.report.items) == 8
    assert spike.report.failures == ()
    outcome = _verify(spike, spike.report)
    assert outcome.passed, [row.code for row in outcome.mismatches]


def test_forged_passed_field_is_rejected_by_contract(spike: FakeSpike) -> None:
    payload = json.loads(spike.report.model_dump_json())
    payload["passed"] = True
    with pytest.raises(ValidationError, match="passed"):
        BuildReport0A.model_validate_json(json.dumps(payload))


def test_forged_raw_field_fails_recomputation(spike: FakeSpike) -> None:
    payload = json.loads(spike.report.model_dump_json())
    payload["items"][0]["observed"]["record_span"]["end_frame"] += 1
    tampered = BuildReport0A.model_validate_json(json.dumps(payload))
    outcome = _verify(spike, tampered)
    assert not outcome.passed
    assert CODE_ITEM_OBSERVED in {row.code for row in outcome.mismatches}


def test_missing_item_readback_fails(tmp_path: Path) -> None:
    broken = run_fake_spike(MANIFEST, FIXTURE_DIR, tmp_path, pool_fault="missing_item")
    assert len(broken.report.items) == 7
    assert any(row.code == "missing-item" for row in broken.report.failures)
    outcome = _verify(broken, broken.report)
    assert not outcome.passed
    assert CODE_ITEM_COUNT in {row.code for row in outcome.mismatches}


def test_decode_failure_fails(tmp_path: Path) -> None:
    broken = run_fake_spike(MANIFEST, FIXTURE_DIR, tmp_path, decode_fails=True)
    outcome = _verify(broken, broken.report)
    assert not outcome.passed
    assert CODE_RENDER_DECODE in {row.code for row in outcome.mismatches}


def test_render_build_mismatch_fails(spike: FakeSpike) -> None:
    spike.render_path.write_bytes(FAKE_RENDER_MAGIC + b"-different-bytes")
    outcome = _verify(spike, spike.report)
    assert not outcome.passed
    assert CODE_RENDER_HASH in {row.code for row in outcome.mismatches}


@pytest.mark.parametrize(
    ("fixture", "observation"),
    [
        ("build-report-forged-success.json", "code=forged-success-rejected"),
        ("build-report-missing-item-readback.json", "code=missing-item-readback"),
        ("build-report-decode-failure.json", "code=render-decode-failed"),
        ("build-report-render-build-mismatch.json", "code=render-hash-mismatch"),
    ],
)
def test_cli_fault_mode_detects_and_cleans_up(fixture: str, observation: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.resolve_bridge.build_report",
            "--manifest",
            str(MANIFEST),
            "--fixture-dir",
            str(FIXTURE_DIR),
        ],
        env=os.environ | {"QA_FAULT_FIXTURE": str(FAULTS / fixture)},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert observation in combined, combined
    assert "owned projects leaked" not in combined
    assert "ERROR: fault" not in combined
