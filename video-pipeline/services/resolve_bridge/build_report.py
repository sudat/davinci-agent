"""Spike Build/Render report writer: one live run into a Build-Report 0A artifact.

Consolidates the raw evidence of a single live spike run (disposable
``__fvp_test__`` project, base cut + fixed presentation from the frozen
manifest, official render job) into the item-level Build-Report 0A contract
artifact. The writer records raw evidence only — pass/fail is always
recomputed by :mod:`services.resolve_bridge.build_report_verify`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.build_report import (
    BuildFailure0A,
    BuildReport0A,
    BuildWarning0A,
    DecodeEvidence0A,
    HostBindings0A,
    RenderJobLifecycle0A,
    RenderOutputEvidence0A,
    RenderProbeSummary0A,
    RenderStreamSummary0A,
)
from services.contracts.primitives import ArtifactRef, Producer, RationalFrameRate
from services.contracts.serialization import GENESIS_SHA256, artifact_content_hash
from services.resolve_bridge.build_report_fingerprint import (
    item_evidence_rows,
    timeline_fingerprint,
)
from services.resolve_bridge.build_report_models import (
    ADAPTER_MODULE,
    SCHEMA_VERSION,
    BindingInputs,
    adapter_version,
    binding_inputs,
)
from services.resolve_bridge.fixed_presentation import run_fixed_presentation
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN, SpikeRunOutcome

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_bridge.base_cut_models import BaseCutRequest
    from services.resolve_bridge.base_cut_plan import ExpectedBaseCut
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.fixed_presentation_models import (
        FfprobeReport,
        RenderEvidence,
    )
    from services.resolve_bridge.fixed_presentation_tools import DecodeOutcome, MediaToolsApi

COMPLETION_NOTE: Final = (
    "job status strings are localized and never parsed; completion is detected only via "
    "CompletionPercentage==100 (Todo-17 capability finding)"
)


class BuildReportError(Exception):
    """The spike run or report assembly violated a required invariant."""


def probe_summary(report: FfprobeReport) -> RenderProbeSummary0A:
    return RenderProbeSummary0A(
        streams=tuple(
            RenderStreamSummary0A.model_validate(stream.model_dump()) for stream in report.streams
        ),
        format_name=report.format.format_name,
        format_duration=report.format.duration,
    )


def render_lifecycle(render: RenderEvidence) -> RenderJobLifecycle0A:
    if not render.job_completed_at or render.poll_count < 1:
        raise BuildReportError(f"render lifecycle incomplete: {render.job_id}")
    return RenderJobLifecycle0A(
        job_id=render.job_id,
        created_at=render.job_created_at,
        started_at=render.job_started_at,
        completed_at=render.job_completed_at,
        poll_count=render.poll_count,
        completion_percentage=render.completion_percentage,
        completion_source="CompletionPercentage",
        note=COMPLETION_NOTE,
    )


def strategy_warnings(run: SpikeRunOutcome) -> tuple[BuildWarning0A, ...]:
    rows: list[BuildWarning0A] = [
        BuildWarning0A(code="render-status-localized", detail=COMPLETION_NOTE)
    ]
    for row in run.report.strategy_table:
        if row.strategy == "direct" and row.status == "verified":
            continue
        rungs = "; ".join(
            f"{attempt.rung}:available={str(attempt.available).lower()}" for attempt in row.ladder
        )
        rows.append(
            BuildWarning0A(
                code=f"strategy-{row.element}-{row.strategy or 'unresolved'}",
                detail=f"{row.reason}; limitations: {row.limitations}; ladder[{rungs}]",
            )
        )
    return tuple(rows)


def run_failures(run: SpikeRunOutcome, decode: DecodeOutcome) -> tuple[BuildFailure0A, ...]:
    rows = [BuildFailure0A(code=row.code, detail=row.detail) for row in run.report.mismatches]
    if decode.exit_code != 0:
        rows.append(
            BuildFailure0A(
                code="render-decode-failed",
                detail=f"decode exit {decode.exit_code}: {decode.stderr_tail[-300:]}",
            )
        )
    return tuple(rows)


def assemble_report(
    run: SpikeRunOutcome,
    expected: ExpectedBaseCut,
    manifest: Phase0AFixtureManifest,
    rate: RationalFrameRate,
    decode: DecodeOutcome,
    bindings: BindingInputs,
) -> BuildReport0A:
    items = item_evidence_rows(expected, run.snapshot, rate, FRAME_ORIGIN)
    output = Path(run.report.render.output_path)
    draft = BuildReport0A(
        artifact_id=f"{manifest.fixture_id}-build-report",
        artifact_type="build_report_0a",
        schema_version=SCHEMA_VERSION,
        content_hash=GENESIS_SHA256,
        producer=Producer(name="resolve-bridge-spike", version=bindings.adapter_version),
        inputs=(
            ArtifactRef(
                artifact_id=f"{manifest.fixture_id}-manifest", sha256=bindings.manifest_sha256
            ),
            ArtifactRef(artifact_id="resolve-host-report", sha256=bindings.host_report_sha256),
        ),
        items=items,
        timeline_fingerprint=timeline_fingerprint(tuple(row.observed for row in items)),
        output_hash=run.report.render.output_sha256,
        render_job=render_lifecycle(run.report.render),
        render_output=RenderOutputEvidence0A(
            output_path=str(output),
            byte_size=output.stat().st_size,
            ffprobe=probe_summary(run.report.render.report),
            decode=DecodeEvidence0A(
                argv=decode.argv, exit_code=decode.exit_code, stderr_tail=decode.stderr_tail
            ),
        ),
        bindings=HostBindings0A(
            host_report_path=str(bindings.host_report_path),
            host_report_sha256=bindings.host_report_sha256,
            resolve_product=bindings.resolve_product,
            resolve_version=bindings.resolve_version,
            resolve_build=bindings.resolve_build,
            adapter_module=ADAPTER_MODULE,
            adapter_version=bindings.adapter_version,
            manifest_path=str(bindings.manifest_path),
            manifest_sha256=bindings.manifest_sha256,
            ffmpeg_path=str(bindings.ffmpeg_bin),
            ffmpeg_sha256=bindings.ffmpeg_sha256,
            ffprobe_path=str(bindings.ffprobe_bin),
            ffprobe_sha256=bindings.ffprobe_sha256,
        ),
        warnings=strategy_warnings(run),
        failures=run_failures(run, decode),
    )
    return draft.model_copy(update={"content_hash": artifact_content_hash(draft)})


def run_spike(
    connection: ResolveConnection,
    request: BaseCutRequest,
    expected: ExpectedBaseCut,
    manifest: Phase0AFixtureManifest,
    media: dict[str, str],
    tools: MediaToolsApi,
    render_dir: Path,
    srt_path: Path,
    *,
    host_report_path: Path,
    manifest_path: Path,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
) -> BuildReport0A:
    run = run_fixed_presentation(
        connection=connection,
        request=request,
        expected=expected,
        manifest=manifest,
        media=media,
        tools=tools,
        render_dir=render_dir,
        srt_path=srt_path,
        direct_allowed=False,
    )
    decode = tools.decode(Path(run.report.render.output_path))
    bindings = binding_inputs(
        connection.binding,
        tools,
        host_report_path=host_report_path,
        manifest_path=manifest_path,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        adapter_ver=adapter_version(),
    )
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    return assemble_report(run, expected, manifest, rate, decode, bindings)


def main() -> int:
    from services.resolve_bridge.build_report_cli import cli_main  # noqa: PLC0415

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
