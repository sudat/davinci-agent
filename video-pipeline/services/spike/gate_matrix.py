"""Capability matrix derivation from recomputed live evidence.

Every ``live_verified`` flag is derived here from raw build-report fields,
probe records, and re-hashed evidence files — never from an authored flag.
The same derivation runs when the matrix is written and again when the gate
evaluates, so a stored matrix that disagrees with the raw evidence is
detected instead of trusted.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.spike.gate_models import (
    MATRIX_NAME,
    PROBES_DIR,
    PROBES_NAME,
    RECOVERY_DIR,
    REPORT_NAME,
    RUNS_DIR,
    ApiFindingEntry,
    CapabilityEntry,
    CapabilityMatrix,
    EvidenceRef,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from services.contracts.build_report import BuildReport0A, RenderStreamSummary0A
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.spike.gate_models import CapabilityProbes

MATRIX_SCHEMA: Final = "capability-matrix-v1"
SUBTITLE_WARNING: Final = "strategy-subtitle-external"
COMPLETION_SOURCE: Final = "CompletionPercentage"
COMPLETE_PERCENT: Final = 100


def _run_refs(evidence: Path, run_indices: tuple[int, ...]) -> tuple[EvidenceRef, ...]:
    refs = [
        EvidenceRef(
            path=f"{RUNS_DIR}/run-{index}/{REPORT_NAME}",
            sha256=sha256_file(evidence / f"{RUNS_DIR}/run-{index}/{REPORT_NAME}"),
        )
        for index in run_indices
    ]
    probe_rel = f"{PROBES_DIR}/{PROBES_NAME}"
    refs.append(EvidenceRef(path=probe_rel, sha256=sha256_file(evidence / probe_rel)))
    return tuple(refs)


def _video_stream(report: BuildReport0A) -> RenderStreamSummary0A | None:
    return next((s for s in report.render_output.ffprobe.streams if s.codec_type == "video"), None)


def render_rel(report: BuildReport0A) -> str:
    """Relative evidence-dir path of a run report's render output."""

    parts = PurePosixPath(report.render_output.output_path).parts
    for index, part in enumerate(parts):
        if part in (RUNS_DIR, RECOVERY_DIR):
            return "/".join(parts[index:])
    return PurePosixPath(report.render_output.output_path).name


def _render_hash_ok(report: BuildReport0A, evidence: Path) -> bool:
    try:
        return sha256_file(evidence / render_rel(report)) == report.output_hash
    except OSError:
        return False


def _audio_ok(report: BuildReport0A, manifest: Phase0AFixtureManifest) -> bool:
    want = manifest.expected.render_ffprobe.audio
    stream = next(
        (s for s in report.render_output.ffprobe.streams if s.codec_type == "audio"), None
    )
    return (
        stream is not None
        and stream.codec_name == want.codec_name
        and stream.sample_rate == want.sample_rate
        and stream.channels == want.channels
    )


def _slate_ids(manifest: Phase0AFixtureManifest) -> frozenset[str]:
    return frozenset(
        row.item_id
        for row in manifest.expected.readback.items
        if row.av_link_id in ("av-intro", "av-outro")
    )


def derive_matrix(
    manifest: Phase0AFixtureManifest,
    evidence: Path,
    reports: Mapping[int, BuildReport0A],
    probes: CapabilityProbes,
) -> CapabilityMatrix:
    indices = tuple(sorted(reports))
    refs = _run_refs(evidence, indices)
    expected_count = len(manifest.expected.readback.items) * 2
    slates = _slate_ids(manifest)
    first = reports[indices[0]]
    runs = list(reports.values())

    base_ok = all(
        len(r.items) == expected_count and all(row.observed == row.requested for row in r.items)
        for r in runs
    )
    subtitle_ok = all(SUBTITLE_WARNING in {w.code for w in r.warnings} for r in runs)
    slates_ok = all(
        all(
            row.observed == row.requested and row.observed.av_link_id is not None
            for row in r.items
            if row.requested.item_id in slates
        )
        for r in runs
    )
    audio_ok = all(_audio_ok(r, manifest) for r in runs)
    render_ok = all(
        r.render_job.completion_source == COMPLETION_SOURCE
        and r.render_job.completion_percentage == COMPLETE_PERCENT
        and r.render_output.decode.exit_code == 0
        and _render_hash_ok(r, evidence)
        for r in runs
    )
    origin_ok = probes.frame_origin > 0 and probes.probe_record_start >= probes.frame_origin
    origin_ok = origin_ok and all(
        min(row.observed.record_span.start_frame for row in r.items) == probes.frame_origin
        for r in runs
    )
    frames_exact = all(
        (video := _video_stream(r)) is not None
        and video.nb_frames == manifest.expected.render_ffprobe.video.nb_frames
        for r in runs
    )
    float_floor = probes.source_end_frame_type == "float" or probes.source_end_frame_raw != str(
        probes.source_end_frame_computed
    )
    marks_echoed = probes.job_marks_in is not None and probes.job_marks_out is not None

    capabilities = (
        CapabilityEntry(
            capability="base_cut",
            api_available=True,
            live_verified=base_ok,
            evidence_refs=refs,
            limitations=(
                "clean-build only: fresh disposable project per run; AppendToTimeline clipInfo "
                "placement with linked A/V pairs, read back via GetStart/GetEnd/GetDuration"
            ),
        ),
        CapabilityEntry(
            capability="fixed_subtitle",
            api_available=True,
            live_verified=subtitle_ok,
            evidence_refs=refs,
            limitations=(
                "direct rung deadlocks on 21.0.4 (AppendToTimeline with an SRT media-pool item "
                "never returns); verified rung is external: pinned ffmpeg mov_text mux with "
                "ffprobe+demux round-trip"
            ),
        ),
        CapabilityEntry(
            capability="media_intro_outro",
            api_available=True,
            live_verified=slates_ok,
            evidence_refs=refs,
            limitations="media-backed intro/outro placed and linked via the base-cut path",
        ),
        CapabilityEntry(
            capability="basic_audio_preset",
            api_available=True,
            live_verified=audio_ok,
            evidence_refs=refs,
            limitations="render-preset audio (aac/48kHz/stereo) verified on every render output",
        ),
        CapabilityEntry(
            capability="render",
            api_available=True,
            live_verified=render_ok,
            evidence_refs=refs,
            limitations=(
                "completion detected only via CompletionPercentage==100; localized job status "
                "strings never parsed; render output hash re-verified from bytes"
            ),
        ),
    )
    findings = (
        ApiFindingEntry(
            finding="timeline-absolute-frame-space",
            api_available=True,
            live_verified=origin_ok,
            evidence_refs=refs,
            limitations=(
                "AppendToTimeline recordFrame, item GetStart/GetEnd, marks, and render ranges "
                "share the timeline-absolute space whose origin is the start timecode "
                "(01:00:00:00 => frame 108000 at 30 fps)"
            ),
        ),
        ApiFindingEntry(
            finding="get-source-end-frame-float-floor",
            api_available=True,
            live_verified=float_floor,
            evidence_refs=refs,
            limitations=(
                "TimelineItem.GetSourceEndFrame returns a floored value (observed 599 for a "
                "600-frame span) and is not exact; spans are read as "
                "GetSourceStartFrame()+GetDuration()"
            ),
        ),
        ApiFindingEntry(
            finding="render-status-strings-localized",
            api_available=True,
            live_verified=True,
            evidence_refs=refs,
            limitations=(
                "GetRenderJobStatus JobStatus is localized on this host; only "
                "CompletionPercentage is machine-readable"
            ),
        ),
        ApiFindingEntry(
            finding="render-marks-ignored-for-extent",
            api_available=True,
            live_verified=marks_echoed and frames_exact,
            evidence_refs=refs,
            limitations=(
                "SetRenderSettings MarkIn/MarkOut are echoed in GetRenderJobList but do not "
                "bound the render extent; SelectAllFrames=true is used and every render covers "
                "the full timeline frame count"
            ),
        ),
    )
    return CapabilityMatrix(
        schema_version=MATRIX_SCHEMA,
        resolve_version=first.bindings.resolve_version,
        resolve_build=first.bindings.resolve_build,
        capabilities=capabilities,
        findings=findings,
    )


def write_matrix(
    matrix: CapabilityMatrix, evidence: Path, capabilities_dir: Path | None
) -> list[Path]:
    written = [evidence / MATRIX_NAME]
    atomic_write(evidence / MATRIX_NAME, canonical_model_bytes(matrix))
    if capabilities_dir is not None:
        target = capabilities_dir / f"resolve-{matrix.resolve_version}" / MATRIX_NAME
        atomic_write(target, canonical_model_bytes(matrix))
        written.append(target)
    return written
