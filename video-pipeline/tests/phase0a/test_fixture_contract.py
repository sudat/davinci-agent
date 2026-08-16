from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest import Phase0AFixtureManifest
from services.fixtures.materialize import load_frozen_manifest
from services.fixtures.materialize_media import _run
from services.fixtures.materialize_validation import (
    MaterializationError,
    MaterializationReport,
)
from services.foundation_io import sha256_file

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
POLICY = Path("config/gates/phase-0a-v1.json")
STATIC_OUTPUTS = (
    "audio-preset.json",
    "expected-readback.json",
    "expected-render-ffprobe.json",
    "expected-source-ffprobe.json",
    "manifest.json",
    "pulse.wav",
    "render-preset.json",
    "slate-silence.wav",
    "subtitle-table.json",
    "subtitle.srt",
    "timeline-ir.json",
)
type JsonValue = bool | int | str | list[JsonValue] | dict[str, JsonValue] | None


def _run_materializer(output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "services.fixtures.materialize",
            "p0a-cfr30-fixed",
            "--policy",
            str(POLICY),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _manifest_payload() -> dict[str, JsonValue]:
    return json.loads(MANIFEST.read_bytes())


def test_materializes_registered_fixture_from_frozen_policy(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    result = _run_materializer(first)
    repeated = _run_materializer(second)

    assert result.returncode == 0, result.stdout + result.stderr
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert (first / "source.mov").is_file()
    assert (first / "materialization-report.json").is_file()
    assert all(sha256_file(first / name) == sha256_file(second / name) for name in STATIC_OUTPUTS)
    first_report = MaterializationReport.model_validate_json(
        (first / "materialization-report.json").read_bytes()
    )
    second_report = MaterializationReport.model_validate_json(
        (second / "materialization-report.json").read_bytes()
    )
    assert first_report.determinism_path == "semantic-equivalence-h264-videotoolbox"
    assert first_report.model_copy(update={"artifacts": ()}) == second_report.model_copy(
        update={"artifacts": ()}
    )
    for root, report in ((first, first_report), (second, second_report)):
        observed_names = {path.name for path in root.iterdir() if path.is_file()}
        assert {artifact.name for artifact in report.artifacts} == observed_names - {
            "materialization-report.json"
        }
        assert all(
            sha256_file(root / artifact.name) == artifact.sha256
            for artifact in report.artifacts
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("generator", "ffmpeg-testsrc-v2", "ffmpeg-testsrc2-bars-v1"),
        ("frame_rate", {"num": 30000, "den": 1001}, "exact CFR 30/1"),
    ],
)
def test_generator_or_vfr_drift_is_rejected(
    field: str, value: JsonValue, message: str
) -> None:
    payload = _manifest_payload()
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    source = recipe["source"]
    assert isinstance(source, dict)
    source[field] = value

    with pytest.raises(ValidationError, match=message):
        Phase0AFixtureManifest.model_validate(payload)


def test_audio_offset_is_rejected() -> None:
    payload = _manifest_payload()
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    audio = recipe["audio"]
    assert isinstance(audio, dict)
    audio["pulse_sample_positions"] = [1024, 241024, 481024, 721024]

    with pytest.raises(ValidationError, match="pulse samples are fixed"):
        Phase0AFixtureManifest.model_validate(payload)


def test_unknown_preset_is_rejected() -> None:
    payload = _manifest_payload()
    recipe = payload["recipe"]
    assert isinstance(recipe, dict)
    preset = recipe["render_preset"]
    assert isinstance(preset, dict)
    preset["preset_id"] = "unknown"

    with pytest.raises(ValidationError, match="phase-0a-h264-aac-v1"):
        Phase0AFixtureManifest.model_validate(payload)


def test_changed_expected_values_cannot_reuse_frozen_hash(tmp_path: Path) -> None:
    payload = _manifest_payload()
    expected = payload["expected"]
    assert isinstance(expected, dict)
    source_probe = expected["source_ffprobe"]
    assert isinstance(source_probe, dict)
    video = source_probe["video"]
    assert isinstance(video, dict)
    video["nb_frames"] = "660"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(MaterializationError, match="hash differs"):
        load_frozen_manifest(changed, sha256_file(MANIFEST))


def test_failed_drift_run_cannot_claim_stale_output_as_success(tmp_path: Path) -> None:
    output = tmp_path / "fixture"
    initial = _run_materializer(output)
    assert initial.returncode == 0
    report_path = output / "materialization-report.json"
    initial_report_hash = sha256_file(report_path)
    policy_payload = json.loads(POLICY.read_bytes())
    policy_payload["fixture_manifest_sha256"] = "0" * 64
    drifted_policy = tmp_path / "drifted-policy.json"
    drifted_policy.write_text(
        json.dumps(policy_payload, sort_keys=True, separators=(",", ":"))
    )

    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.fixtures.materialize",
            "p0a-cfr30-fixed",
            "--policy",
            str(drifted_policy),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 2
    assert "fixture materialized and verified" not in failed.stdout + failed.stderr
    assert sha256_file(report_path) == initial_report_hash
    retained = MaterializationReport.model_validate_json(report_path.read_bytes())
    assert retained.policy_sha256 == sha256_file(POLICY)
    assert retained.policy_sha256 != sha256_file(drifted_policy)


def test_media_commands_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def expire(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["ffmpeg"], timeout=90)

    monkeypatch.setattr(subprocess, "run", expire)

    with pytest.raises(MaterializationError, match="90 second limit"):
        _run(("ffmpeg",))
