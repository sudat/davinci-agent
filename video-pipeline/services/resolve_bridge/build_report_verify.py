"""Forge-resistant verification of a Build-Report 0A artifact.

Never reads a stored pass flag (the contract has none): every assertion is
recomputed from raw fields, the frozen manifest, the host report file, and the
render bytes. Item assertions are two equations — ``requested ==
manifest-derived expected`` and ``observed == requested`` — so tampering
either side fails with an explicit mismatch code.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.primitives import RationalFrameRate
from services.contracts.serialization import artifact_content_hash
from services.resolve_bridge.base_cut_plan import expected_from_manifest, fixture_media_map
from services.resolve_bridge.build_report_fingerprint import (
    requested_placement,
    timeline_fingerprint,
)
from services.resolve_bridge.build_report_models import (
    CODE_BINDING_HOST,
    CODE_BINDING_MANIFEST,
    CODE_BINDING_RESOLVE,
    CODE_BINDING_TOOL,
    CODE_CONTENT_HASH,
    CODE_FAILURES,
    CODE_FINGERPRINT,
    CODE_ITEM_COUNT,
    CODE_ITEM_OBSERVED,
    CODE_ITEM_REQUESTED,
    CODE_RENDER_DECODE,
    CODE_RENDER_HASH,
    CODE_RENDER_JOB,
    CODE_RENDER_OUTPUT,
    CODE_RENDER_PROBE,
    COMPLETE_PERCENT,
    Flags,
    VerifyOutcome,
    VerifyPort,
    probe_matches,
)
from services.resolve_bridge.fixed_presentation_models import (
    FRAME_ORIGIN,
    RenderEvidence,
)
from services.resolve_bridge.fixed_presentation_render import compare_render
from services.resolve_bridge.readiness import ResolveHostReport

if TYPE_CHECKING:
    from services.contracts.build_report import BuildReport0A
    from services.fixtures.manifest import Phase0AFixtureManifest





def verify_report(
    report: BuildReport0A,
    manifest: Phase0AFixtureManifest,
    fixture_dir: Path,
    port: VerifyPort,
    *,
    manifest_path: Path,
    host_report_path: Path,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
) -> VerifyOutcome:
    flags = Flags()
    _verify_envelope(report, flags)
    _verify_items(report, manifest, fixture_dir, flags)
    _verify_render_job(report, flags)
    _verify_output(report, manifest, port, flags)
    _verify_bindings(report, port, manifest_path, host_report_path, ffmpeg_bin, ffprobe_bin, flags)
    if report.failures:
        flags.add(CODE_FAILURES, "; ".join(f"{row.code}: {row.detail}" for row in report.failures))
    return flags.outcome()


def _verify_envelope(report: BuildReport0A, flags: Flags) -> None:
    recomputed = artifact_content_hash(report)
    if recomputed != report.content_hash:
        flags.add(CODE_CONTENT_HASH, f"{report.content_hash} != {recomputed}")


def _verify_items(
    report: BuildReport0A,
    manifest: Phase0AFixtureManifest,
    fixture_dir: Path,
    flags: Flags,
) -> None:
    media = fixture_media_map(fixture_dir)
    expected = expected_from_manifest(manifest, media)
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    by_id = {(row.requested.item_id, row.requested.kind): row for row in report.items}
    if len(by_id) != len(report.items):
        flags.add(CODE_ITEM_COUNT, "duplicate item ids in report")
    if len(report.items) != len(expected.items):
        flags.add(
            CODE_ITEM_COUNT,
            f"manifest expects {len(expected.items)} items, report carries {len(report.items)}",
        )
    for want in expected.items:
        row = by_id.get((want.item_id, want.kind))
        if row is None:
            flags.add(CODE_ITEM_COUNT, f"item {want.item_id} missing from report")
            continue
        if row.requested != requested_placement(want, rate, FRAME_ORIGIN):
            flags.add(CODE_ITEM_REQUESTED, f"{want.item_id}: requested != manifest-derived")
        if row.observed != row.requested:
            flags.add(CODE_ITEM_OBSERVED, f"{want.item_id}: observed != requested")
    fingerprint = timeline_fingerprint(tuple(row.observed for row in report.items))
    if fingerprint != report.timeline_fingerprint:
        flags.add(CODE_FINGERPRINT, f"{report.timeline_fingerprint} != {fingerprint}")


def _verify_render_job(report: BuildReport0A, flags: Flags) -> None:
    job = report.render_job
    if job.completion_percentage != COMPLETE_PERCENT or job.completion_source != (
        "CompletionPercentage"
    ):
        flags.add(
            CODE_RENDER_JOB,
            f"completion {job.completion_source}={job.completion_percentage}; "
            "only CompletionPercentage==100 proves completion",
        )
        return
    stamps: list[datetime] = []
    for label, value in (
        ("created_at", job.created_at),
        ("started_at", job.started_at),
        ("completed_at", job.completed_at),
    ):
        try:
            stamps.append(datetime.fromisoformat(value))
        except ValueError as error:
            flags.add(CODE_RENDER_JOB, f"{label} is not ISO-8601: {value!r} ({error})")
            return
    if _ordered(stamps):
        return
    flags.add(
        CODE_RENDER_JOB,
        f"lifecycle timestamps out of order: {job.created_at} -> {job.started_at} -> "
        f"{job.completed_at}",
    )


def _ordered(stamps: list[datetime]) -> bool:
    try:
        return stamps[0] <= stamps[1] <= stamps[2]
    except TypeError:
        return False


def _verify_output(
    report: BuildReport0A,
    manifest: Phase0AFixtureManifest,
    port: VerifyPort,
    flags: Flags,
) -> None:
    output = Path(report.render_output.output_path)
    if not output.is_file():
        flags.add(CODE_RENDER_OUTPUT, f"render output missing: {output}")
        return
    digest = port.sha256(output)
    if digest != report.output_hash:
        flags.add(CODE_RENDER_HASH, f"render bytes hash {digest} != reported {report.output_hash}")
    size = output.stat().st_size
    if size != report.render_output.byte_size:
        flags.add(CODE_RENDER_HASH, f"render byte size {size} != reported byte size")
    fresh = port.probe(output)
    if not probe_matches(report.render_output.ffprobe, fresh):
        flags.add(CODE_RENDER_PROBE, "recorded ffprobe summary != fresh probe of render bytes")
    evidence = RenderEvidence(
        job_id=report.render_job.job_id,
        status="complete",
        output_path=str(output),
        output_sha256=report.output_hash,
        report=fresh,
        marks_in=None,
        marks_out=None,
    )
    _, render_mismatches = compare_render(evidence, manifest)
    for mismatch in render_mismatches:
        flags.add(mismatch.code, mismatch.detail)
    decode = port.decode(output)
    if decode.exit_code != 0:
        flags.add(
            CODE_RENDER_DECODE,
            f"decode exit {decode.exit_code}: {decode.stderr_tail[-300:] or 'no stderr'}",
        )
    recorded_decode = report.render_output.decode.exit_code
    if recorded_decode != 0:
        flags.add(CODE_RENDER_DECODE, f"recorded decode exit {recorded_decode}")


def _verify_bindings(
    report: BuildReport0A,
    port: VerifyPort,
    manifest_path: Path,
    host_report_path: Path,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    flags: Flags,
) -> None:
    bindings = report.bindings
    host_digest = port.sha256(host_report_path)
    if host_digest != bindings.host_report_sha256:
        flags.add(
            CODE_BINDING_HOST, f"host report hash {host_digest} != {bindings.host_report_sha256}"
        )
    manifest_digest = port.sha256(manifest_path)
    if manifest_digest != bindings.manifest_sha256:
        flags.add(
            CODE_BINDING_MANIFEST, f"manifest hash {manifest_digest} != {bindings.manifest_sha256}"
        )
    try:
        host = ResolveHostReport.model_validate_json(host_report_path.read_bytes())
    except (OSError, ValueError) as error:
        flags.add(CODE_BINDING_HOST, f"host report unreadable: {error}")
        return
    if (
        bindings.resolve_version != host.application.version
        or bindings.resolve_build != host.application.build
    ):
        flags.add(
            CODE_BINDING_RESOLVE,
            f"report binds {bindings.resolve_version} build {bindings.resolve_build}; "
            f"host report says {host.application.version} build {host.application.build}",
        )
    for label, bound_path, bound_hash, actual_bin in (
        ("ffmpeg", bindings.ffmpeg_path, bindings.ffmpeg_sha256, ffmpeg_bin),
        ("ffprobe", bindings.ffprobe_path, bindings.ffprobe_sha256, ffprobe_bin),
    ):
        actual_hash = port.sha256(actual_bin)
        if Path(bound_path).resolve() != actual_bin.resolve() or bound_hash != actual_hash:
            flags.add(
                CODE_BINDING_TOOL,
                f"{label} binding {bound_path} != {actual_bin} or hash mismatch",
            )
