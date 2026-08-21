from __future__ import annotations

import argparse
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.execution.work_init import WorkInitializationError, restore_for_plan
from services.fixtures.manifest import Phase0AFixtureManifest
from services.fixtures.materialize_media import (
    decoded_video_sha256,
    encode_slate,
    encode_source,
    probe_media,
    write_audio,
)
from services.fixtures.materialize_validation import (
    ArtifactHash,
    AudioPreset,
    MaterializationError,
    MaterializationReport,
    MaterializedTimeline,
    parse_probe,
    subtitle_bytes,
    validate_probe,
    validate_pulse,
    validate_slate,
)
from services.fixtures.models import FreezeReceipt
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates.verify_policy import PolicyVerificationError, verify_policy
from services.toolchain.models import LockError, load_lock
from services.toolchain.verify import verify_binary

FIXTURE_ID: Final = "p0a-cfr30-fixed"


@dataclass(frozen=True, slots=True)
class StageBindings:
    stage: Path
    ffmpeg: Path
    ffprobe: Path
    policy_sha256: str
    manifest_sha256: str
    golden_expected_sha256: str


def _receipt_for_policy(attempt_dir: Path, policy: Path) -> Path:
    """The freeze receipt matching the policy's gate version (cascade-aware)."""
    import json  # noqa: PLC0415

    payload = json.loads(policy.read_bytes())
    version = payload.get("gate_version")
    if isinstance(version, str) and version != "v1":
        cascaded = attempt_dir / "gate-cascade" / "receipts" / f"phase-0a-{version}.json"
        if cascaded.is_file():
            return cascaded
    return attempt_dir / "task-6-freeze-receipt.json"


def _load_receipt(path: Path) -> FreezeReceipt:
    raw = path.read_bytes()
    receipt = FreezeReceipt.model_validate_json(raw)
    if raw != canonical_model_bytes(receipt):
        raise MaterializationError("freeze receipt is noncanonical")
    return receipt


def load_frozen_manifest(path: Path, expected_sha256: str) -> Phase0AFixtureManifest:
    if sha256_file(path) != expected_sha256:
        raise MaterializationError("fixture manifest hash differs from frozen policy")
    raw = path.read_bytes()
    manifest = Phase0AFixtureManifest.model_validate_json(raw)
    if raw != manifest.canonical_bytes():
        raise MaterializationError("fixture manifest is noncanonical")
    return manifest


def _write_contracts(stage: Path, manifest: Phase0AFixtureManifest) -> None:
    atomic_write(stage / "manifest.json", manifest.canonical_bytes())
    atomic_write(stage / "subtitle.srt", subtitle_bytes(manifest))
    atomic_write(stage / "subtitle-table.json", canonical_model_bytes(manifest.recipe.subtitle))
    timeline = MaterializedTimeline(
        frame_rate=manifest.recipe.source.frame_rate,
        items=manifest.expected.readback.items,
        subtitle=manifest.recipe.subtitle,
    )
    audio_preset = AudioPreset(
        audio_codec=manifest.recipe.render_preset.audio_codec,
        audio_sample_rate=manifest.recipe.render_preset.audio_sample_rate,
        audio_channels=manifest.recipe.render_preset.audio_channels,
    )
    atomic_write(stage / "timeline-ir.json", canonical_model_bytes(timeline))
    atomic_write(stage / "audio-preset.json", canonical_model_bytes(audio_preset))
    atomic_write(stage / "render-preset.json", canonical_model_bytes(manifest.recipe.render_preset))
    atomic_write(
        stage / "expected-source-ffprobe.json",
        canonical_model_bytes(manifest.expected.source_ffprobe),
    )
    atomic_write(
        stage / "expected-readback.json", canonical_model_bytes(manifest.expected.readback)
    )
    atomic_write(
        stage / "expected-render-ffprobe.json",
        canonical_model_bytes(manifest.expected.render_ffprobe),
    )


def _materialize_stage(
    bindings: StageBindings,
    manifest: Phase0AFixtureManifest,
) -> None:
    stage = bindings.stage
    write_audio(stage, manifest)
    encode_source(bindings.ffmpeg, stage, manifest)
    encode_slate(
        bindings.ffmpeg, stage, manifest, "intro", manifest.recipe.intro.color
    )
    encode_slate(
        bindings.ffmpeg, stage, manifest, "outro", manifest.recipe.outro.color
    )
    _write_contracts(stage, manifest)
    validate_pulse(stage / "pulse.wav", manifest.recipe.audio)
    source_probe = parse_probe(probe_media(bindings.ffprobe, stage / "source.mov"))
    intro_probe = parse_probe(probe_media(bindings.ffprobe, stage / "intro.mov"))
    outro_probe = parse_probe(probe_media(bindings.ffprobe, stage / "outro.mov"))
    validate_probe(source_probe, manifest.expected.source_ffprobe)
    validate_slate(intro_probe, manifest)
    validate_slate(outro_probe, manifest)
    artifacts = tuple(
        ArtifactHash(name=path.name, sha256=sha256_file(path))
        for path in sorted(stage.iterdir())
        if path.is_file()
    )
    report = MaterializationReport(
        schema_version="phase-0a-materialization-report-v1",
        fixture_id=FIXTURE_ID,
        policy_sha256=bindings.policy_sha256,
        fixture_manifest_sha256=bindings.manifest_sha256,
        golden_expected_sha256=bindings.golden_expected_sha256,
        determinism_path="semantic-equivalence-h264-videotoolbox",
        codec_variability=(
            "h264_videotoolbox bytes are not asserted stable; frozen ffprobe, PCM, "
            "subtitle, timeline, preset, and expected tables define equivalence"
        ),
        source_probe=source_probe,
        intro_probe=intro_probe,
        outro_probe=outro_probe,
        source_decoded_video_sha256=decoded_video_sha256(
            bindings.ffmpeg, stage / "source.mov"
        ),
        intro_decoded_video_sha256=decoded_video_sha256(
            bindings.ffmpeg, stage / "intro.mov"
        ),
        outro_decoded_video_sha256=decoded_video_sha256(
            bindings.ffmpeg, stage / "outro.mov"
        ),
        artifacts=artifacts,
        validation="passed",
    )
    atomic_write(stage / "materialization-report.json", canonical_model_bytes(report))


def materialize(fixture_id: str, policy: Path, output_dir: Path | None) -> Path:
    if fixture_id != FIXTURE_ID:
        raise MaterializationError(f"unknown fixture: {fixture_id}")
    pipeline_root = Path(__file__).resolve().parents[2]
    workspace_root = pipeline_root.parent
    record = restore_for_plan(
        workspace_root / ".omo/start-work/ledger.jsonl",
        workspace_root / ".omo/plans/foundation-video-pipeline.md",
        "",
    )
    receipt_path = _receipt_for_policy(record.attempt_dir, policy)
    receipt = _load_receipt(receipt_path)
    verify_policy(policy, receipt_path, None, Path(receipt.execution_contract_path))
    lock_path = Path(receipt.toolchain_lock_path)
    lock = load_lock(lock_path)
    verify_binary(lock.ffmpeg.ffmpeg)
    verify_binary(lock.ffmpeg.ffprobe)
    manifest_path = Path(receipt.fixture_manifest_path)
    manifest = load_frozen_manifest(manifest_path, receipt.fixture_manifest_sha256)
    golden_expected = pipeline_root / "tests/goldens/reference/phase-0a/expected.json"
    if sha256_file(golden_expected) != receipt.golden_hashes.expected_sha256:
        raise MaterializationError("Golden expected hash differs from freeze receipt")
    destination = output_dir or record.attempt_dir / "phase-0a/fixture"
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".p0a-fixture-", dir=destination.parent))
    try:
        _materialize_stage(
            StageBindings(
                stage=stage,
                ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
                ffprobe=Path(lock.ffmpeg.ffprobe.path),
                policy_sha256=sha256_file(policy),
                manifest_sha256=receipt.fixture_manifest_sha256,
                golden_expected_sha256=receipt.golden_hashes.expected_sha256,
            ),
            manifest,
        )
        if destination.is_symlink():
            raise MaterializationError("fixture output directory cannot be a symlink")
        if destination.exists():
            shutil.rmtree(destination)
        stage.replace(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture_id")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        output = materialize(arguments.fixture_id, arguments.policy, arguments.output_dir)
    except (
        LockError,
        MaterializationError,
        OSError,
        PolicyVerificationError,
        ValidationError,
        WorkInitializationError,
    ) as error:
        print(error)
        return 2
    print(f"fixture materialized and verified: {FIXTURE_ID} -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
