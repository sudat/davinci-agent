"""Synthetic Phase-2 evidence models: reports, verdicts, renders, outputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import (
    BuildOutput,
    FfprobeSummary,
    ItemReadbackRow,
    RenderResult,
    SubtitleResult,
)
from services.build.conformance_models import ConformanceTable, ItemVerdict
from services.job_runner.gate_p2_finalize import fixture_privacy_declarations
from services.job_runner.gate_p2_ir import manifest_for
from services.qc.models import (
    QcInputBinding,
    QcReport,
    QcToolVersions,
    UnresolvedHumanGate,
)
from services.qc.privacy_gate import evaluate_privacy_gate

if TYPE_CHECKING:
    from services.job_runner.gate_p2_models import P2FixtureObservation
    from services.resolve_adapter.models import ResolvePackage

FAULTS: Final = (
    "missing_approval",
    "missing_qc_readback",
    "item_mismatch",
    "manual_ui_dependency",
    "unbounded_retry",
    "synthetic_as_real_approval",
)
EXPECTED_CODES: Final[dict[str, str]] = {
    "missing_approval": "final-approval-missing",
    "missing_qc_readback": "qc-evidence-missing",
    "item_mismatch": "item-conformance-defect",
    "manual_ui_dependency": "manual-ui-dependency",
    "unbounded_retry": "unbounded-retry",
    "synthetic_as_real_approval": "synthetic-as-real-approval",
}
FIXTURE_IDS: Final = (
    "p2-stale-capability",
    "p2-partial-build-restart",
    "p2-same-duration-wrong-media",
    "p2-false-render-complete",
    "p2-blocking-qc-privacy",
)


@dataclass(frozen=True, slots=True)
class FaultKnobs:
    fault: str = ""


@dataclass(slots=True)
class SynthTree:
    evidence: Path
    observations: dict[str, P2FixtureObservation]
    knobs: FaultKnobs


def _versions() -> QcToolVersions:
    return QcToolVersions(
        qc_engine="phase2-gate-fake-v1",
        ffmpeg_sha256="0" * 64,
        ffprobe_sha256="0" * 64,
    )


def _passed_report(render_sha: str) -> QcReport:
    return QcReport(
        schema_version="qc-report-v1",
        verdict="passed",
        issues=(),
        unresolved_human_gates=(),
        tool_versions=_versions(),
        threshold_version="qc-thresholds-fake-v1",
        inputs=(QcInputBinding(kind="render", sha256=render_sha),),
    )


def _privacy_report(render_sha: str) -> QcReport:
    issues, gates = evaluate_privacy_gate(
        fixture_privacy_declarations(manifest_for("p2-blocking-qc-privacy")),
        "qc-thresholds-fake-v1",
        (render_sha,),
    )
    return QcReport(
        schema_version="qc-report-v1",
        verdict="blocked",
        issues=tuple(issues),
        unresolved_human_gates=tuple(
            UnresolvedHumanGate.model_validate(gate.model_dump(mode="json")) for gate in gates
        ),
        tool_versions=_versions(),
        threshold_version="qc-thresholds-fake-v1",
        inputs=(QcInputBinding(kind="render", sha256=render_sha),),
    )


def _verdicts(package: ResolvePackage, *, break_one: bool) -> tuple[ItemVerdict, ...]:
    verdicts: list[ItemVerdict] = []
    inject = break_one
    for placement in package.placements:
        info = placement.clip_info
        length = info.end_frame - info.start_frame
        record_end_observed = info.record_frame + length + (1 if inject else 0)
        verdicts.append(
            ItemVerdict(
                item_id=placement.item_id,
                observed=True,
                media_match=True,
                media_sha256_expected=package.inputs_view.declared_media[0].sha256,
                media_sha256_observed=package.inputs_view.declared_media[0].sha256,
                media_path_expected="synthetic-edit-source.mov",
                media_path_observed="synthetic-edit-source.mov",
                span_match=not inject,
                source_start_expected=info.start_frame,
                source_start_observed=info.start_frame,
                source_end_expected=info.end_frame,
                source_end_observed=info.end_frame,
                record_start_expected=info.record_frame,
                record_start_observed=info.record_frame,
                record_end_expected=info.record_frame + length,
                record_end_observed=record_end_observed,
                record_end_delta_frames=1 if inject else 0,
                track_match=True,
                track_kind_expected="video" if info.track_type == "video" else "audio",
                track_kind_observed="video" if info.track_type == "video" else "audio",
                track_index_expected=info.track_index,
                track_index_observed=info.track_index,
                link_match=True,
                passed=not inject,
                detail="" if not inject else "synthetic injected item mismatch",
                faults=() if not inject else ("off_by_one",),
            )
        )
        inject = False
    return tuple(verdicts)


def _table(package: ResolvePackage, *, break_one: bool = False) -> ConformanceTable:
    verdicts = _verdicts(package, break_one=break_one)
    fingerprint = package.content_hash
    total = sum(v.record_end_expected - v.record_start_expected for v in verdicts)
    return ConformanceTable(
        timeline_name="__fvp_test__synthetic",
        package_artifact_id=package.artifact_id,
        items=verdicts,
        extra_rows=(),
        expected_items=len(verdicts),
        observed_items=len(verdicts),
        missing_item_ids=(),
        expected_total_record_frames=total,
        observed_total_record_frames=total,
        total_record_delta_frames=0,
        expected_fingerprint=fingerprint,
        observed_fingerprint=fingerprint,
        all_passed=bool(verdicts) and all(v.passed for v in verdicts),
    )


def _render_file(work: Path, fixture_id: str) -> tuple[Path, str]:
    render = work / "render" / "final.mp4"
    render.parent.mkdir(parents=True, exist_ok=True)
    payload = f"synthetic-render-{fixture_id}".encode()
    render.write_bytes(payload)
    return render, hashlib.sha256(payload).hexdigest()


def _summary() -> FfprobeSummary:
    return FfprobeSummary(
        format_name="mov,mp4",
        duration="20.0",
        video_codec="h264",
        width=1920,
        height=1080,
        r_frame_rate="30/1",
        nb_frames="600",
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=2,
    )


def _build_output(
    fixture_id: str, package: ResolvePackage, render: Path, sha: str, *, break_one: bool = False
) -> BuildOutput:
    table = _table(package, break_one=break_one)
    rows = tuple(
        ItemReadbackRow(
            item_id=v.item_id,
            kind=v.track_kind_observed,
            track_index=v.track_index_observed,
            record_start=v.record_start_observed,
            record_end=v.record_end_observed,
            source_start=v.source_start_observed,
            source_end=v.source_end_observed,
            media_path=v.media_path_observed,
            linked_ids=v.linked_observed,
            passed=v.passed,
            detail=v.detail,
        )
        for v in table.items
    )
    subtitle = (
        SubtitleResult(
            srt_sha256="0" * 64,
            output_path=str(render),
            output_sha256=sha,
            codec="mov_text",
        )
        if fixture_id == "p2-blocking-qc-privacy"
        else None
    )
    return BuildOutput(
        package_artifact_id=package.artifact_id,
        package_content_hash=package.content_hash,
        project_name=f"__fvp_test__build_{package.content_hash[:12]}",
        timeline_name="__fvp_test__synthetic",
        timeline_fingerprint=table.expected_fingerprint,
        swept_projects=(),
        items=rows,
        conformance=table,
        render=RenderResult(
            job_id=f"synthetic-{fixture_id}",
            output_path=str(render),
            output_sha256=sha,
            completion_percentage=100,
            poll_count=3,
            probe=_summary(),
        ),
        subtitle=subtitle,
    )




__all__ = ["EXPECTED_CODES", "FAULTS", "FIXTURE_IDS", "FaultKnobs", "SynthTree"]
