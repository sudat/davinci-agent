"""NormalizeRecord assembly from a verified normalization run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.primitives import ArtifactRef, Producer
from services.normalize.models import (
    DropDupAccounting,
    DropDupExpectation,
    FileIdentity,
    NormalizationPolicy,
    NormalizeRecord,
    OutputSemantics,
    ReplayPolicy,
    ToolIdentity,
)
from services.normalize.verify import target_profile_from_lock

if TYPE_CHECKING:
    from services.conform.rate_model import CfrConversionReport
    from services.ingest.models import SourceManifest
    from services.normalize.probe import MediaFacts
    from services.toolchain.models import Phase0BToolchainLock

PRODUCER_NAME = "services.normalize"
PRODUCER_VERSION = "1"
REPLAY_NOTE = (
    "container bytes are not asserted stable across replays; identical argv, "
    "ffprobe fields, frame accounting, and the decoded-frame hash define "
    "semantic equivalence (Phase-0A precedent)"
)


@dataclass(frozen=True, slots=True)
class VerifiedRun:
    """Everything a verified normalization run contributes to its record."""

    source_manifest: SourceManifest
    source: Path
    source_sha256: str
    output: Path
    output_sha256: str
    argv: tuple[str, ...]
    lock: Phase0BToolchainLock
    lock_sha256: str
    prediction: CfrConversionReport
    output_facts: MediaFacts
    decoded_video_sha256: str
    declared_video_pix_fmt: str | None = None
    declared_conversions: tuple[str, ...] = ()


def build_normalize_record(run: VerifiedRun) -> NormalizeRecord:
    return NormalizeRecord(
        schema_version="normalize-record-v1",
        artifact_type="normalize-record",
        artifact_id=f"normalize-record-{run.output_sha256[:16]}",
        content_hash="0" * 64,
        producer=Producer(name=PRODUCER_NAME, version=PRODUCER_VERSION),
        inputs=(
            ArtifactRef(
                artifact_id=run.source_manifest.artifact_id,
                sha256=run.source_manifest.content_hash,
            ),
        ),
        source=FileIdentity(
            path=str(run.source.resolve()),
            sha256=run.source_sha256,
            size_bytes=run.source.stat().st_size,
        ),
        output=FileIdentity(
            path=str(run.output.resolve()),
            sha256=run.output_sha256,
            size_bytes=run.output.stat().st_size,
        ),
        argv=run.argv,
        tool=ToolIdentity(
            ffmpeg_sha256=run.lock.ffmpeg.ffmpeg.sha256,
            ffprobe_sha256=run.lock.ffmpeg.ffprobe.sha256,
            lock_sha256=run.lock_sha256,
        ),
        target=target_profile_from_lock(run.lock.normalization.target),
        policy=NormalizationPolicy(
            rotation="noautorotate-rotation-metadata-preserved-v1",
            color="preserve-or-explicit-v1",
        ),
        declared_video_pix_fmt=run.declared_video_pix_fmt,
        declared_conversions=run.declared_conversions,
        drop_dup=DropDupAccounting(
            expected=DropDupExpectation(
                output_frames=run.prediction.output_frames,
                dropped=tuple(run.prediction.dropped_source_frames),
                duplicated=tuple(run.prediction.duplicated_source_frames),
            ),
            basis="conform-frame-conversion-accounting-v1",
        ),
        output_semantics=OutputSemantics(
            decoded_video_sha256=run.decoded_video_sha256,
            observed_output_frames=run.output_facts.video.nb_read_frames,
        ),
        replay=ReplayPolicy(
            determinism="semantic-equivalence-h264-videotoolbox", note=REPLAY_NOTE
        ),
    )
