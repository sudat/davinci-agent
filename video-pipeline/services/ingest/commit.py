"""Manifest assembly, content-hash sealing, and verification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.primitives import Producer
from services.foundation_io import canonical_model_bytes, sha256_file
from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    Eligibility,
    EligibilityReason,
    FileIdentity,
    ProbeRecord,
    RecipePointer,
    SourceManifest,
    StreamMonotonicity,
    VfrEvidence,
    VideoStreamRecord,
)
from services.ingest.records import container_info

if TYPE_CHECKING:
    from services.ingest.ingest import AnalysisResult

PRODUCER = Producer(name="services.ingest", version="1")
PROBE_ARGUMENTS = ("-v", "error", "-print_format", "json", "-show_streams", "-show_format")
CONTENT_HASH_PLACEHOLDER = "0" * 64
UNKNOWN_CONTAINER = ContainerInfo(
    format_name="unknown", format_long_name=None, nb_streams=0, duration_num=0, duration_den=1
)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    original: Path
    size: int
    hash_before: str
    payload: dict[str, object] | None
    analysis: AnalysisResult | None
    corrupt: str | None
    changed: bool


def build_manifest(
    outcome: ProbeOutcome, recipe: RecipePointer, ffprobe: Path
) -> SourceManifest:
    streams: tuple[VideoStreamRecord | AudioStreamRecord, ...] = ()
    monotonic: tuple[StreamMonotonicity, ...] = ()
    vfr: VfrEvidence | None = None
    reasons: tuple[EligibilityReason, ...] = ()
    if outcome.changed:
        analysis = outcome.analysis
        streams = analysis.streams if analysis is not None else ()
        monotonic = analysis.monotonic if analysis is not None else ()
        vfr = analysis.vfr if analysis is not None else None
        reasons = (
            EligibilityReason(
                code="changed_original_detected",
                detail="original hash drifted while it was being probed",
            ),
        )
    elif outcome.corrupt is not None:
        reasons = (
            EligibilityReason(
                code="corrupt_decode",
                detail=f"ffprobe could not demux/decode: {outcome.corrupt}",
            ),
        )
    elif outcome.analysis is not None:
        streams = outcome.analysis.streams
        monotonic = outcome.analysis.monotonic
        vfr = outcome.analysis.vfr
        reasons = outcome.analysis.reasons
    return SourceManifest(
        schema_version="source-manifest-v1",
        artifact_type="source-manifest",
        artifact_id=f"source-manifest-{outcome.hash_before[:16]}",
        content_hash=CONTENT_HASH_PLACEHOLDER,
        producer=PRODUCER,
        file=FileIdentity(
            path=str(outcome.original.resolve()),
            size_bytes=outcome.size,
            sha256=outcome.hash_before,
        ),
        container=(
            container_info(outcome.payload)
            if outcome.payload is not None
            else UNKNOWN_CONTAINER
        ),
        streams=streams,
        monotonicity=monotonic,
        vfr_evidence=vfr,
        edit_source_recipe=recipe,
        eligibility=Eligibility(
            verdict="blocked" if reasons else "supported", reasons=reasons
        ),
        probe=ProbeRecord(
            ffprobe_path=str(ffprobe),
            ffprobe_sha256=sha256_file(ffprobe),
            arguments=PROBE_ARGUMENTS,
        ),
    )


def seal_manifest(manifest: SourceManifest) -> SourceManifest:
    placeholder = manifest.model_copy(update={"content_hash": CONTENT_HASH_PLACEHOLDER})
    digest = hashlib.sha256(canonical_model_bytes(placeholder)).hexdigest()
    return manifest.model_copy(update={"content_hash": digest})


def verify_manifest_hash(manifest: SourceManifest) -> bool:
    placeholder = manifest.model_copy(update={"content_hash": CONTENT_HASH_PLACEHOLDER})
    return hashlib.sha256(canonical_model_bytes(placeholder)).hexdigest() == manifest.content_hash
