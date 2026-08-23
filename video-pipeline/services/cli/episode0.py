"""``python -m services.cli.episode0`` — Episode-0 baseline + phase rerun tooling.

Subcommands:
  freeze-manifest — hash/size/duration for real-01 source (from episode.json)
  report          — operator log + manifest → runs/<run-id>/report.json
  compare         — delta between two reports (file paths or run labels)
  run             — phase rerun harness:
                     ``run --phase editorial-v2`` executes brief →
                     media-intelligence v2 → Director v2 three-pass
                     (llm_call=None) → validation+commit → synthetic Review
                     Event correction → Timeline IR v2 → Editorial Preview →
                     measurement report + Gate V43-2 checklist.
                     ``run --phase full-build`` (task 43) continues past the
                     editorial stages: presentation intents (T32, within
                     density) → subtitle/audio/color plans (T33/T34/T35) →
                     kit selections (T37) → Timeline IR v2 → McpExecutionPlan
                     (T38) → T39 runner with a FAKE executor
                     (``--executor live`` probes the pinned MCP server first
                     and BLOCKS on refusal) → quality domains (T40) →
                     editorial QC (T41) → presentation preview stub (T60) →
                     PublishabilityReview input scaffold (T42) → report +
                     Gate V43-3 checklist + legacy rollback leg.
"""

# allow: SIZE_OK — tasks 30+43 pin the rerun-harness commit scope to this T5
# CLI module plus metrics/episode0_gate_v43_2.py + metrics/episode0_gate_v43_3.py
# (all contracts live there). The added code is one responsibility — rerun
# orchestration — and each stage is a thin delegate to an existing service.
# T29/T31 single-module precedent.

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, get_args

from pydantic import BaseModel, ValidationError

from services.config.backends import BackendsConfigError, load_backends, set_backend
from services.contracts.primitives import RationalFrameRate
from services.creative_plan.audio_finishing import (
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    AudioFinishingPlanV1,
    AudioOpRequestV1,
    build_audio_plan,
    load_capability_statuses,
)
from services.creative_plan.color_finishing import (
    ColorFactsV1,
    ColorFinishingPlanV1,
    ColorIssueV1,
    ColorPlanPolicy,
    McpCapabilityStatus,
    build_color_plan,
)
from services.creative_plan.compile_ir_v2 import (
    SourceFactsV2,
    SourceFactV2,
    TranscriptFactV2,
    compile_ir_v2,
)
from services.creative_plan.edit_models_v2 import (
    CreativeEditPlanProposalV2,
    SelectionRefV2,
    upgrade_from_creative_draft,
)
from services.creative_plan.ir_models_v2 import TimelineIrV2
from services.creative_plan.presentation_intents import (
    PresentationIntentV2,
    PunchInParams,
    attach_presentation_intents,
    check_density,
    load_default_profile,
)
from services.creative_plan.quality_domains import (
    DomainExecutionV1,
    ExecutionFactsV1,
    QualityDomainName,
    QualityDomainReportV1,
    QualityFactsV1,
    QualityPlansV1,
    build_domain_report,
)
from services.creative_plan.subtitle_models import AsrSegmentV1, SubtitlePlanV1
from services.creative_plan.subtitle_plan import build_subtitle_plan
from services.editorial_v2.director_v2 import DirectorV2
from services.editorial_v2.episode_brief import (
    EpisodeBriefV1,
    require_approved,
)
from services.editorial_v2.evidence_v2 import assemble_evidence_v2
from services.editorial_v2.moment_models import MomentSelectionProposalV2
from services.editorial_v2.proposal_validate import (
    MomentSelectionStore,
    commit_selection,
    initialize_moment_store,
    validate_proposal,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import McpCallRecorder
from services.mcp_client.client import McpClient, McpToolCallError
from services.mcp_client.errors import McpClientError
from services.mcp_execution.compiler import compile_execution_plan
from services.mcp_execution.plan_payloads import AudioMetricReadback
from services.mcp_execution.runner import (
    McpExecutionRunnerV2,
    McpExecutionRunReportV1,
)
from services.media_intelligence.models import MediaIntelligenceArtifact
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2
from services.metrics.episode0_baseline import (
    REPORT_SCHEMA,
    Episode0BaselineLogV1,
    Episode0ReportV1,
    Episode0SourceManifest,
    compare_reports,
    compute_manifest_for_episode_json,
    generate_report,
)
from services.metrics.episode0_gate_v43_2 import (
    RERUN_REPORT_SCHEMA,
    ArtifactFileRef,
    Episode0RerunReportV1,
    GateEvidenceV1,
    RerunCommitV1,
    RerunEditorialMetricsV1,
    RerunInputsV1,
    RerunPreviewV1,
    RerunReviewCorrectionV1,
    RerunTasteV1,
    RerunThreePassV1,
    RerunValidationV1,
    check_gate,
    compare_rerun,
)
from services.metrics.episode0_gate_v43_3 import (
    BuildExecutionV1,
    BuildKitSelectionV1,
    BuildPlansV1,
    BuildPreviewV1,
    BuildPublishabilityV1,
    BuildQualityV1,
    BuildRenderRecordV1,
    BuildRollbackV1,
    Episode0FullBuildReportV1,
    FullBuildGateCheckV1,
    FullBuildGateEvidenceV1,
    KitSelectionsArtifactV1,
    PresentationIntentsArtifactV1,
    PublishabilityScaffoldV1,
)
from services.metrics.episode0_gate_v43_3 import (
    check_gate as check_gate_v43_3,
)
from services.preview.errors import PreviewError
from services.preview.tools import load_pinned_tools
from services.preview.v2_editorial import render_editorial_preview
from services.preview.v2_models import SourceMediaEntryV2, SourceMediaMapV2
from services.preview.v2_presentation import (
    McpExecutionPlanStub,
    StubStepV2,
    render_presentation_preview,
)
from services.production_kit.recipe_select import select_recipe
from services.production_kit.registry import ChannelProductionKitV1, load_kit
from services.qc.editorial_checks import (
    EditorialQcInput,
    aggregate_candidates,
    run_editorial_qc,
)
from services.reference_learning.models import DerivedTasteProfileV1
from services.review_command.events import MOMENT_SELECTION_V2_COMMITTED
from services.review_command.store import load_events

if TYPE_CHECKING:
    from services.editorial_v2.director_v2 import ThreePassResult
    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.proposal_validate import CommitReceipt, ValidationResult
    from services.editorial_v2.story_plan import StoryPlanV1
    from services.mcp_client.execution_runner import McpTransportFn
    from services.mcp_execution.plan_models import (
        McpExecutionPlanV1,
        McpExecutionStepV1,
    )

DEFAULT_EPISODE_JSON = Path("private/reference-episodes/real-01/episode.json")
DEFAULT_RUNS_ROOT = Path("private/reference-episodes/real-01/runs")
DEFAULT_BACKENDS = Path("config/backends.json")
DEFAULT_MCP_PIN = Path("config/toolchains/davinci-resolve-mcp.pin.json")
ExecutorName = Literal["fake", "live"]
_MIN_KEEPS_FOR_CORRECTION = 2
PHASE_0C_LOCK = Path("config/toolchains/phase-0c-v1.json")
_FULL_BUILD_JOB_ID = "dev-episode0-full-build"
_FULL_BUILD_STAGE = "stage-build"
#: A punch-in needs a >=30 s timeline to stay within the default profile's
#: 2.0/min cap (1 intent / (30/60) min == 2.0/min). Shorter timelines carry
#: an honest EMPTY within-density intent set instead of a violating one.
_PUNCH_IN_MIN_SECONDS = 30.0
#: Which T39 plan-step actions belong to which quality domain (execution
#: adapter seam documented in quality_domains.py).
_DOMAIN_ACTIONS: Mapping[QualityDomainName, frozenset[str]] = {
    "subtitle": frozenset({"apply_subtitles"}),
    "audio_finishing": frozenset(
        {"apply_audio_stage", "apply_ducking", "apply_audio_op", "apply_voice_isolation"}
    ),
    "color_finishing": frozenset({"apply_color"}),
    "framing_motion": frozenset({"apply_transform"}),
    "graphics_presentation": frozenset({"place_title"}),
}


class Episode0BlockedError(Exception):
    """Gate V43-0.5 precondition unmet — the rerun must not start."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class Episode0RerunError(Exception):
    """Typed rerun-input refusal (unreadable/invalid inputs)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.episode0",
        description="Episode-0 longitudinal baseline + phase rerun tooling.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze-manifest", help="freeze source manifest from episode.json")
    freeze.add_argument("--episode-json", type=Path, default=DEFAULT_EPISODE_JSON)
    freeze.add_argument("--out", type=Path, default=None, help="write manifest json to this path")

    report = sub.add_parser("report", help="operator log + manifest → report.json")
    report.add_argument("--log", type=Path, required=True, help="path to episode0-baseline-v1 json")
    report.add_argument("--run-id", type=str, required=True, help="run id (baseline or ISO date)")
    report.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="precomputed manifest json; if omitted, derived from --episode-json",
    )
    report.add_argument("--episode-json", type=Path, default=DEFAULT_EPISODE_JSON)
    report.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)

    compare = sub.add_parser("compare", help="delta between two reports (paths or run labels)")
    compare.add_argument("--a", type=Path, required=True, help="report path or run label")
    compare.add_argument("--b", type=Path, required=True, help="report path or run label")
    compare.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    compare.add_argument("--out", type=Path, default=None, help="write delta json to this path")

    run = sub.add_parser("run", help="execute a phase rerun harness on one episode")
    run.add_argument("--phase", required=True, choices=["editorial-v2", "full-build"])
    run.add_argument("--episode", required=True, help="reference episode id (e.g. real-01)")
    run.add_argument("--episode-root", type=Path, default=None)
    run.add_argument("--runs-root", type=Path, default=None)
    run.add_argument("--brief", type=Path, required=True, help="approved episode-brief-v1 json")
    run.add_argument("--mi-artifact", type=Path, required=True, help="media-intelligence-v2 json")
    run.add_argument("--taste-profile", type=Path, default=None, help="taste profile json")
    run.add_argument("--source-media", type=Path, default=None, help="preview edit-source media")
    run.add_argument("--frame-rate", type=str, default="30/1", help="edit-source rate as num/den")
    run.add_argument("--backends", type=Path, default=DEFAULT_BACKENDS)
    run.add_argument("--run-id", type=str, default=None, help="default <date>-<phase>")
    run.add_argument(
        "--executor",
        choices=["fake", "live"],
        default="fake",
        help="full-build executor: fake (synthetic actuals, self-test) or live "
        "(pinned MCP server; probed first, BLOCKED on refusal)",
    )
    run.add_argument(
        "--pin",
        type=Path,
        default=DEFAULT_MCP_PIN,
        help="davinci-resolve-mcp pin contract (live executor only)",
    )

    return parser


# ----------------------------------------------------------- freeze/report/compare


def _cmd_freeze_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = compute_manifest_for_episode_json(args.episode_json)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"freeze_failed: {exc}", file=sys.stderr)
        return 1
    payload = canonical_model_bytes(manifest)
    if args.out is not None:
        atomic_write(args.out, payload)
        print(f"manifest: {args.out} sha256={manifest.sha256[:12]} size={manifest.size_bytes}")
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        log_data: object = json.loads(args.log.read_text(encoding="utf-8"))
        log = Episode0BaselineLogV1.model_validate(log_data)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"log_unreadable: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"log_invalid: {exc}", file=sys.stderr)
        return 1

    try:
        if args.manifest is not None:
            raw: object = json.loads(args.manifest.read_text(encoding="utf-8"))
            manifest = Episode0SourceManifest.model_validate(raw)
        else:
            manifest = compute_manifest_for_episode_json(args.episode_json)
    except (OSError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        print(f"manifest_unreadable: {exc}", file=sys.stderr)
        return 1

    try:
        out = generate_report(log, manifest, run_id=args.run_id, runs_root=args.runs_root)
    except (ValueError, OSError, ValidationError) as exc:
        print(f"report_failed: {exc}", file=sys.stderr)
        return 1
    print(f"report: {out} run_id={args.run_id}")
    return 0


def _resolve_report_arg(value: Path, runs_root: Path) -> Path | None:
    """A report argument is either an existing file or a run label under runs_root."""

    if value.is_file():
        return value
    labeled = runs_root / value.name / "report.json"
    return labeled if labeled.is_file() else None


def _emit_delta(delta: dict[str, object], out: Path | None) -> None:
    payload = (
        json.dumps(delta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )
    if out is not None:
        atomic_write(out, payload)
        print(f"delta: {out}")
    else:
        sys.stdout.buffer.write(payload)


def _cmd_compare(args: argparse.Namespace) -> int:
    a_path = _resolve_report_arg(args.a, args.runs_root)
    if a_path is None:
        return _comparison_unavailable("a", args.a, args.runs_root, args.out)
    b_path = _resolve_report_arg(args.b, args.runs_root)
    if b_path is None:
        return _comparison_unavailable("b", args.b, args.runs_root, args.out)
    try:
        b_doc: object = json.loads(b_path.read_text(encoding="utf-8"))
        b_schema = b_doc.get("schema_version") if isinstance(b_doc, dict) else None
        if b_schema == RERUN_REPORT_SCHEMA:
            baseline = Episode0ReportV1.model_validate(
                json.loads(a_path.read_text(encoding="utf-8"))
            )
            delta = compare_rerun(baseline, Episode0RerunReportV1.model_validate(b_doc))
        elif b_schema == REPORT_SCHEMA:
            delta = compare_reports(a_path, b_path)
        else:
            print(f"compare_failed: unknown report schema for --b: {b_schema!r}", file=sys.stderr)
            return 1
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(f"compare_failed: {exc}", file=sys.stderr)
        return 1
    _emit_delta(delta, args.out)
    return 0


def _comparison_unavailable(label: str, value: Path, runs_root: Path, out: Path | None) -> int:
    """中間run比較の欠落は「比較不能」表示のみ — never a block (plan task 30)."""

    _emit_delta(
        {
            "comparison": "unavailable",
            "reason": (
                f"report for --{label} not found: {value} "
                f"(runs_root={runs_root}) — 比較不能 (display only, not a block)"
            ),
        },
        out,
    )
    return 0


# ----------------------------------------------------------- rerun harness (task 30)


@dataclass(frozen=True, slots=True)
class _RunInputs:
    reference_episode_id: str
    run_id: str
    run_dir: Path
    brief_path: Path
    mi_path: Path
    taste_path: Path | None
    source_media: Path | None
    rate: RationalFrameRate


@dataclass(frozen=True, slots=True)
class _StageDeps:
    inputs: _RunInputs
    api: MediaQueryApiV2
    artifact: MediaIntelligenceArtifact
    source_id: str
    store: MomentSelectionStore


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Episode0RerunError("input-unreadable", f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise Episode0RerunError("input-invalid-json", f"{path} is not valid JSON: {exc}") from exc


def _parse_frame_rate(text: str) -> RationalFrameRate:
    head, _, tail = text.partition("/")
    try:
        num = int(head)
        den = int(tail) if tail else 1
    except ValueError as exc:
        raise Episode0RerunError(
            "invalid-frame-rate", f"--frame-rate must be num/den: {text!r}"
        ) from exc
    if num <= 0 or den <= 0:
        raise Episode0RerunError("invalid-frame-rate", f"--frame-rate must be positive: {text!r}")
    return RationalFrameRate(num=num, den=den)


def _require_valid_run_id(value: str) -> str:
    if "/" in value or "\\" in value or not value:
        raise ValueError(f"invalid run id {value!r}")
    return value


def _check_baseline_precondition(runs_root: Path) -> None:

    path = runs_root / "baseline" / "report.json"
    if not path.is_file():
        raise Episode0BlockedError(
            "baseline-missing",
            f"Gate V43-0.5 unmet — baseline report not found at {path}. ESCALATION to "
            "operator: record the Episode-0 baseline first (python -m services.cli.episode0 "
            "report --log ... --run-id baseline) with a non-null active_human_time_minutes, "
            "then rerun. The editorial-v2 rerun was NOT started.",
        )
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Episode0BlockedError(
            "baseline-unreadable", f"Gate V43-0.5 unmet — cannot read {path}: {exc}"
        ) from exc
    log = payload.get("log") if isinstance(payload, dict) else None
    aht = log.get("active_human_time_minutes") if isinstance(log, dict) else None
    if not isinstance(aht, int | float) or isinstance(aht, bool):
        raise Episode0BlockedError(
            "baseline-aht-unmeasured",
            f"Gate V43-0.5 unmet — baseline active_human_time_minutes is not a measured "
            f"number (got {aht!r}) in {path}. ESCALATION to operator: the baseline AHT "
            "must be measured before the editorial-v2 rerun may start.",
        )


def _append_run_event(run_dir: Path, event: str, **fields: object) -> None:
    entry = {"event": event, "ts": datetime.now(tz=UTC).isoformat(), **fields}
    with (run_dir / "run-events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def _artifact_ref(run_dir: Path, name: str, model: BaseModel) -> ArtifactFileRef:
    path = run_dir / name
    atomic_write(path, canonical_model_bytes(model))
    return ArtifactFileRef(path=str(path), sha256=sha256_file(path))


def _input_ref(path: Path) -> ArtifactFileRef:
    return ArtifactFileRef(path=str(path), sha256=sha256_file(path))


def _stage_director(
    brief: EpisodeBriefV1, api: MediaQueryApiV2, taste: DerivedTasteProfileV1 | None
) -> ThreePassResult:
    return DirectorV2().run_three_pass(brief, api, taste_profile=taste, llm_call=None)


@dataclass(frozen=True, slots=True)
class _CommitOutcome:
    proposal: MomentSelectionProposalV2
    receipt: CommitReceipt
    bundle: EvidenceBundleV2
    validation: ValidationResult


def _stage_commit(deps: _StageDeps, result: ThreePassResult) -> _CommitOutcome:
    proposal = result.moment_selection.proposal
    bundle = assemble_evidence_v2(deps.api, proposal.candidates, source_id=deps.source_id)
    validation = validate_proposal(proposal, deps.api, bundle)
    initialize_moment_store(deps.store, episode_id=proposal.episode_id)
    receipt = commit_selection(proposal, validation, store=deps.store)
    return _CommitOutcome(proposal, receipt, bundle, validation)


def _stage_correct(
    deps: _StageDeps, result: ThreePassResult, committed: _CommitOutcome
) -> tuple[MomentSelectionProposalV2, CommitReceipt | None, str | None]:
    """Apply one synthetic review correction via the T29 store (keep → optional)."""

    proposal = committed.proposal
    targeted = {
        intent.target_candidate_id
        for intent in result.creative_edit.intents
        if intent.target_candidate_id is not None
    }
    keeps = [c for c in proposal.candidates if c.intent == "keep"]
    eligible = [c for c in keeps if c.candidate_type != "speech" and c.candidate_id not in targeted]
    if len(keeps) < _MIN_KEEPS_FOR_CORRECTION or not eligible:
        return proposal, None, None
    target = eligible[-1]
    corrected_candidate = target.model_copy(
        update={
            "intent": "optional",
            "rationale": (
                "synthetic review correction via structured Review Event (task-30 harness)"
            ),
        }
    )
    corrected = MomentSelectionProposalV2(
        proposal_id=proposal.proposal_id,
        episode_id=proposal.episode_id,
        candidates=tuple(
            corrected_candidate if c.candidate_id == target.candidate_id else c
            for c in proposal.candidates
        ),
    )
    validation = validate_proposal(corrected, deps.api, committed.bundle)
    receipt = commit_selection(corrected, validation, store=deps.store)
    return corrected, receipt, target.candidate_id


@dataclass(frozen=True, slots=True)
class _CompileOutcome:
    plan: CreativeEditPlanProposalV2
    ir: TimelineIrV2
    source_facts: SourceFactsV2


def _stage_compile(
    deps: _StageDeps, result: ThreePassResult, proposal: MomentSelectionProposalV2
) -> _CompileOutcome:
    if len(deps.artifact.sources) != 1:
        raise Episode0RerunError(
            "multi-source-unsupported",
            f"editorial-v2 rerun harness supports single-source episodes, got "
            f"{len(deps.artifact.sources)} sources",
        )
    source = deps.artifact.sources[0]
    assert source.duration_frames is not None  # noqa: S101 (narrowing guard, T30)
    operations = upgrade_from_creative_draft(
        result.creative_edit,
        proposal,
        b_roll_matches=result.moment_selection.b_roll_matches,
    )
    plan = CreativeEditPlanProposalV2(
        schema_version="creative-edit-plan-v2",
        proposal_id=f"cep-{proposal.proposal_id}",
        episode_id=proposal.episode_id,
        selection_ref=SelectionRefV2(proposal_id=proposal.proposal_id),
        operations=operations,
    )
    facts = SourceFactsV2(
        rate=deps.inputs.rate,
        sources=(
            SourceFactV2(
                source_id=source.source_id,
                duration_frames=source.duration_frames,
            ),
        ),
        transcripts=tuple(
            TranscriptFactV2(
                segment_id=segment.segment_id,
                source_id=source.source_id,
                text=segment.text,
                start_frame=segment.start_frame,
                end_frame=segment.end_frame,
            )
            for shot in deps.artifact.shots
            for segment in (shot.transcript_segments or ())
        ),
    )
    return _CompileOutcome(
        plan=plan,
        ir=compile_ir_v2(proposal, plan, source_facts=facts),
        source_facts=facts,
    )


def _stage_preview(deps: _StageDeps, ir: TimelineIrV2) -> RerunPreviewV1:
    if deps.inputs.source_media is None:
        return RerunPreviewV1(skipped_reason="no source media supplied (pass --source-media)")
    try:
        tools = load_pinned_tools(PHASE_0C_LOCK)
    except (PreviewError, OSError) as exc:
        raise Episode0RerunError("preview-toolchain-unavailable", str(exc)) from exc
    media = deps.inputs.source_media
    media_map = SourceMediaMapV2(
        entries=(
            SourceMediaEntryV2(
                source_id=deps.source_id,
                media_path=str(media.resolve()),
                sha256=sha256_file(media),
            ),
        )
    )
    trace = render_editorial_preview(
        ir,
        subtitle_plan=None,
        source_media_map=media_map,
        output_path=deps.inputs.run_dir / "editorial-preview.mp4",
        tools=tools,
    )
    return RerunPreviewV1(
        preview_path=trace.preview.path,
        preview_sha256=trace.preview.sha256,
        total_record_frames=trace.total_record_frames,
    )


@dataclass(frozen=True, slots=True)
class _EditorialOutcome:
    """Everything downstream build stages consume from the editorial head."""

    report: Episode0RerunReportV1
    evidence: GateEvidenceV1
    artifact: MediaIntelligenceArtifact
    source_facts: SourceFactsV2
    ir: TimelineIrV2
    selection: MomentSelectionProposalV2
    story_plan: StoryPlanV1
    creative_plan: CreativeEditPlanProposalV2


def _run_editorial_stages(inputs: _RunInputs) -> _EditorialOutcome:
    brief = require_approved(EpisodeBriefV1.model_validate(_load_json(inputs.brief_path)))
    artifact = MediaIntelligenceArtifact.model_validate(_load_json(inputs.mi_path))
    taste = (
        None
        if inputs.taste_path is None
        else DerivedTasteProfileV1.model_validate(_load_json(inputs.taste_path))
    )
    index_path = inputs.run_dir / "media-intelligence.duckdb"
    build_index(artifact, index_path)
    with MediaQueryApiV2.open(index_path) as api:
        deps = _StageDeps(
            inputs=inputs,
            api=api,
            artifact=artifact,
            source_id=artifact.sources[0].source_id,
            store=MomentSelectionStore(plan_dir=inputs.run_dir / "moment-selection"),
        )
        result = _stage_director(brief, api, taste)
        committed = _stage_commit(deps, result)
        corrected, correction_receipt, corrected_id = _stage_correct(deps, result, committed)
        compile_outcome = _stage_compile(deps, result, corrected)
        plan = compile_outcome.plan
        ir = compile_outcome.ir
        preview = _stage_preview(deps, ir)

        three_pass = RerunThreePassV1(
            story_plan=_artifact_ref(inputs.run_dir, "story-plan.json", result.story_plan),
            moment_selection=_artifact_ref(
                inputs.run_dir, "moment-selection.json", result.moment_selection
            ),
            creative_edit=_artifact_ref(inputs.run_dir, "creative-edit.json", result.creative_edit),
            creative_plan=_artifact_ref(inputs.run_dir, "creative-plan.json", plan),
            timeline_ir=_artifact_ref(inputs.run_dir, "timeline-ir.json", ir),
        )
        citations = tuple(
            dict.fromkeys(
                (*result.moment_selection.taste_citations, *result.creative_edit.taste_citations)
            )
        )
        candidates = corrected.candidates
        editorial = RerunEditorialMetricsV1(
            story_plan_block_count=len(result.story_plan.blocks),
            selection_candidate_count=len(candidates),
            non_speech_candidate_count=sum(1 for c in candidates if c.candidate_type != "speech"),
            kept_non_speech_count=sum(
                1 for c in candidates if c.intent == "keep" and c.candidate_type != "speech"
            ),
            kept_speech_count=sum(
                1 for c in candidates if c.intent == "keep" and c.candidate_type == "speech"
            ),
            removed_count=sum(1 for c in candidates if c.intent == "remove"),
            evidence_coverage_rate=len(committed.bundle.entries) / len(candidates),
        )
        committed_events = load_events(deps.store.log_path)
        evidence = GateEvidenceV1(
            story_plan_block_count=len(result.story_plan.blocks),
            selection_candidate_count=len(candidates),
            non_speech_candidate_count=editorial.non_speech_candidate_count,
            review_committed_event_count=sum(
                1 for e in committed_events if e.kind == MOMENT_SELECTION_V2_COMMITTED
            ),
        )
        report = Episode0RerunReportV1(
            schema_version=RERUN_REPORT_SCHEMA,
            run_id=inputs.run_id,
            phase="editorial-v2",
            episode_id=brief.episode_id,
            inputs=RerunInputsV1(
                reference_episode_id=inputs.reference_episode_id,
                brief=_input_ref(inputs.brief_path),
                media_intelligence=_input_ref(inputs.mi_path),
                taste_profile=None if inputs.taste_path is None else _input_ref(inputs.taste_path),
                source_media=(
                    None if inputs.source_media is None else _input_ref(inputs.source_media)
                ),
                frame_rate=f"{inputs.rate.num}/{inputs.rate.den}",
            ),
            three_pass=three_pass,
            validation=RerunValidationV1(
                candidates_checked=committed.validation.candidates_checked,
                refs_verified=committed.validation.refs_verified,
                hallucination_count=0,
            ),
            commit=RerunCommitV1(
                version=committed.receipt.version,
                event_id=committed.receipt.event_id,
                proposal_sha256=committed.receipt.proposal_sha256,
            ),
            review_correction=RerunReviewCorrectionV1(
                exercised=correction_receipt is not None,
                synthetic=True,
                version_from=None if correction_receipt is None else committed.receipt.version,
                version_to=None if correction_receipt is None else correction_receipt.version,
                event_id=None if correction_receipt is None else correction_receipt.event_id,
                corrected_candidate_id=corrected_id,
                note="synthetic keep→optional demotion applied via the T29 Review Event store",
            ),
            taste=RerunTasteV1(
                profile_supplied=taste is not None,
                explicitly_absent=taste is None,
                citation_count=len(citations),
                citations=citations,
            ),
            editorial=editorial,
            preview=preview,
        )
    return _EditorialOutcome(
        report=report,
        evidence=evidence,
        artifact=artifact,
        source_facts=compile_outcome.source_facts,
        ir=ir,
        selection=corrected,
        story_plan=result.story_plan,
        creative_plan=plan,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    episode_root = args.episode_root or Path(f"private/reference-episodes/{args.episode}")
    runs_root = args.runs_root or episode_root / "runs"
    try:
        _check_baseline_precondition(runs_root)
    except Episode0BlockedError as exc:
        print(f"blocked: {exc.code}: {exc.detail}", file=sys.stderr)
        print(
            "BLOCKED — Gate V43-0.5 unmet; escalated to operator. Not continuing.", file=sys.stderr
        )
        return 1
    run_id = args.run_id or f"{datetime.now(tz=UTC).date().isoformat()}-{args.phase}"
    try:
        _require_valid_run_id(run_id)
        rate = _parse_frame_rate(args.frame_rate)
        previous = load_backends(args.backends).editorial_contract
    except (Episode0RerunError, ValueError, BackendsConfigError) as exc:
        print(f"run_failed: {exc}", file=sys.stderr)
        return 1
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs = _RunInputs(
        reference_episode_id=args.episode,
        run_id=run_id,
        run_dir=run_dir,
        brief_path=args.brief,
        mi_path=args.mi_artifact,
        taste_path=args.taste_profile,
        source_media=args.source_media,
        rate=rate,
    )
    if args.phase == "full-build":
        return _cmd_run_full_build(args, inputs, previous)
    return _cmd_run_editorial_v2(args, inputs, previous)


def _cmd_run_editorial_v2(args: argparse.Namespace, inputs: _RunInputs, previous: str) -> int:
    run_dir = inputs.run_dir
    try:
        set_backend("editorial_contract", "multimodal_v2", path=args.backends)
    except BackendsConfigError as exc:
        print(f"run_failed: cannot switch editorial_contract: {exc}", file=sys.stderr)
        return 1
    _append_run_event(
        run_dir,
        "flag_switched",
        key="editorial_contract",
        **{"from": previous, "to": "multimodal_v2"},
    )
    print(f"editorial_contract: {previous} -> multimodal_v2 (backends={args.backends})")
    report: Episode0RerunReportV1 | None = None
    evidence: GateEvidenceV1 | None = None
    try:
        outcome = _run_editorial_stages(inputs)
        report, evidence = outcome.report, outcome.evidence
    except Exception as exc:  # noqa: BLE001 (CLI boundary funnel: named failure, exit 1)
        print(f"run_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        failure: BaseException | None = exc
    else:
        failure = None
    finally:
        try:
            set_backend("editorial_contract", previous, path=args.backends)
            _append_run_event(
                run_dir,
                "flag_restored",
                key="editorial_contract",
                **{"from": "multimodal_v2", "to": previous},
            )
            print(f"editorial_contract: multimodal_v2 -> {previous} (restored)")
        except BackendsConfigError as exc:
            print(f"flag_restore_failed: {exc}", file=sys.stderr)
    if failure is not None:
        return 1
    assert report is not None  # type: ignore[assert-type]  # narrowed by failure == None branch  # noqa: S101
    assert evidence is not None  # noqa: S101
    report_path = run_dir / "report.json"
    atomic_write(report_path, canonical_model_bytes(report))
    gate = check_gate(report, evidence)
    gate_path = run_dir / "gate-check.json"
    atomic_write(gate_path, canonical_model_bytes(gate))
    print(f"report: {report_path}")
    print(f"gate-check: {gate_path} passed={gate.passed}")
    return 0 if gate.passed else 1


# ------------------------------------------------------ full-build (task 43)


@dataclass(frozen=True, slots=True)
class _FullBuildOutcome:
    report: Episode0FullBuildReportV1
    editorial_evidence: GateEvidenceV1
    gate: FullBuildGateCheckV1


def _live_client_from_pin(pin_path: Path) -> McpClient:
    """Seam for tests/other transports; production builds from the pin file."""

    return McpClient.from_pin(pin_path)


def _probe_live_executor(pin_path: Path) -> McpTransportFn:
    """Connect the pinned server + Resolve, or raise a typed BLOCKED."""

    client = _live_client_from_pin(pin_path)
    try:
        client.connect()
        client.resolve_get_version()
    except (McpClientError, OSError, ValueError) as exc:
        raise Episode0BlockedError(
            "mcp-server-unreachable",
            f"live executor requested but the pinned MCP server / Resolve is not "
            f"reachable ({type(exc).__name__}: {exc}). ESCALATION to operator: start "
            f"DaVinci Resolve and the pinned davinci-resolve-mcp server (pin="
            f"{pin_path}), then rerun. The full-build run was NOT started.",
        ) from exc

    def executor(tool_name: str, action: str, normalized_params: Mapping[str, object]) -> object:
        response = client.transport.request(
            "tools/call",
            {"name": tool_name, "arguments": {"action": action, "params": dict(normalized_params)}},
        )
        result = response.get("result") if isinstance(response, dict) else None
        envelope = result if isinstance(result, dict) else {}
        if envelope.get("isError"):
            raise McpToolCallError(tool_name, f"server reported error: {envelope!r}")
        content = envelope.get("content")
        text = content[0].get("text", "") if content and isinstance(content, list) else ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpToolCallError(tool_name, f"{action} payload is not JSON") from exc

    return executor


class _SyntheticPlanExecutor:
    """Fake MCP executor: replays each step's expected readback as the actual.

    Steps are queued per action in plan order (the runner dispatches in plan
    order), so each dispatch maps to its own step. No server, no I/O; the
    readback verifier still compares structure field by field.
    """

    def __init__(self, plan: McpExecutionPlanV1) -> None:
        self._queues: dict[str, list[McpExecutionStepV1]] = {}
        for step in plan.steps:
            self._queues.setdefault(step.action, []).append(step)

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],  # noqa: ARG002 (protocol shape)
    ) -> object:
        queue = self._queues.get(action)
        if not queue:
            raise McpToolCallError(tool_name, f"unexpected action dispatched: {action}")
        step = queue.pop(0)
        actual = step.expected_readback.model_dump(mode="json")
        if isinstance(step.expected_readback, AudioMetricReadback):
            # the verifier wants a measured value within [minimum, maximum]
            actual["value"] = step.expected_readback.minimum
        return actual


def _note_prior_editorial_run(inputs: _RunInputs) -> None:
    """editorial rerun #1's report is the natural prior; absence is 比較不能 (display only)."""

    found: str | None = None
    for path in sorted(inputs.run_dir.parent.glob("*/report.json"), reverse=True):
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == RERUN_REPORT_SCHEMA:
            found = path.parent.name
            break
    if found is None:
        print("prior editorial-v2 run: none — 比較不能 (display only, not a block)")
        _append_run_event(inputs.run_dir, "prior_editorial_run", found=False, note="比較不能")
    else:
        print(f"prior editorial-v2 run: {found}")
        _append_run_event(inputs.run_dir, "prior_editorial_run", found=True, run_id=found)


def _timeline_seconds(ir: TimelineIrV2) -> float:
    last = max(
        (item.record_span.end_frame for track in ir.video_tracks for item in track.items),
        default=0,
    )
    return last * ir.rate.den / ir.rate.num


def _author_presentation_intents(ir: TimelineIrV2) -> tuple[PresentationIntentV2, ...]:
    """At most one synthetic punch-in, and only when the profile caps allow it."""

    primary = next((t for t in ir.video_tracks if t.role == "primary"), None)
    if primary is None or not primary.items:
        return ()
    if _timeline_seconds(ir) < _PUNCH_IN_MIN_SECONDS:
        return ()
    span = primary.items[0].record_span
    return (
        PresentationIntentV2(
            intent_id="pi-001",
            kind="emphasis_punch_in",
            target_span=span,
            params=PunchInParams(scale=1.4, duration_frames=30),
            rationale="synthetic within-density punch-in authored by the full-build harness",
        ),
    )


def _asr_segments(
    artifact: MediaIntelligenceArtifact, rate: RationalFrameRate
) -> tuple[AsrSegmentV1, ...]:
    """Single-source harness (enforced by _stage_compile): frames → seconds."""

    source_id = artifact.sources[0].source_id
    seconds = rate.den / rate.num
    return tuple(
        AsrSegmentV1(
            segment_id=segment.segment_id,
            source_id=source_id,
            text=segment.text,
            start_seconds=segment.start_frame * seconds,
            end_seconds=segment.end_frame * seconds,
        )
        for shot in artifact.shots
        for segment in (shot.transcript_segments or ())
    )


def _color_policy() -> ColorPlanPolicy:
    statuses = load_capability_statuses()
    valid = get_args(McpCapabilityStatus)
    grade = statuses["color-grade-preset-drx"]
    qc = statuses["advanced-delivery-qc"]
    if grade not in valid or qc not in valid:
        raise Episode0RerunError(
            "invalid-matrix-status",
            f"color capability statuses must be one of {valid}: "
            f"color-grade-preset-drx={grade!r} advanced-delivery-qc={qc!r}",
        )
    return ColorPlanPolicy(
        color_grade_status=cast("McpCapabilityStatus", grade),
        advanced_qc_status=cast("McpCapabilityStatus", qc),
    )


def _kit_selections(
    kit: ChannelProductionKitV1,
    *,
    subtitle_plan: SubtitlePlanV1,
    audio_plan: AudioFinishingPlanV1,
    color_plan: ColorFinishingPlanV1,
) -> tuple[BuildKitSelectionV1, ...]:
    """Resolve the ACTIVE domain recipes through the kit (never ad-hoc)."""

    wanted: list[str] = []
    if subtitle_plan.cues:
        wanted.append("subtitle_track")
    if any(
        op.op == "voice_isolation" and op.enabled for stage in audio_plan.stages for op in stage.ops
    ):
        wanted.append("voice_isolation")
    if (
        color_plan.technical_correction.exposure.needed
        or color_plan.technical_correction.white_balance.needed
    ):
        wanted.append("color_technical_normalize")
    return tuple(_kit_selection(kit, intent) for intent in wanted)


def _kit_selection(kit: ChannelProductionKitV1, intent: str) -> BuildKitSelectionV1:
    selection = select_recipe(kit, intent)
    recipe = selection.recipe
    return BuildKitSelectionV1(
        intent=intent,
        recipe_id=recipe.recipe_id,
        origin=recipe.provenance.origin,
        license=recipe.provenance.license,
        fallback=recipe.fallback,
        rationale=selection.selection_rationale,
        resolved_params=selection.resolved_params,
    )


def _execute_plan(
    plan: McpExecutionPlanV1,
    *,
    run_dir: Path,
    backends_path: Path,
    executor: McpTransportFn,
    executor_name: ExecutorName,
) -> McpExecutionRunReportV1:
    store = StateStore.open(run_dir / "job-state.sqlite3")
    try:
        store.acquire_lease(
            resource=stage_resource(_FULL_BUILD_JOB_ID, _FULL_BUILD_STAGE),
            holder="episode0-harness",
            now=1000,
            ttl_seconds=60,
        )
        runner = McpExecutionRunnerV2(
            store=store,
            job_id=_FULL_BUILD_JOB_ID,
            stage_name=_FULL_BUILD_STAGE,
            holder_token="episode0-harness",  # noqa: S106 (lease token, not a credential)
            ledger_dir=run_dir / "mcp-call-ledger",
            backends_path=backends_path,
            clock=SequenceClock(1000),
        )
        recorder = McpCallRecorder(
            provider_version="2.98.3",
            resolve_version="21.0.4",
            server_mode=executor_name,
            clock=lambda: 0,
        )
        return runner.execute(plan, executor=executor, recorder=recorder)
    finally:
        store.close()


def _rollback_leg(run_dir: Path, backends_path: Path) -> BuildRollbackV1:
    """Flip execution_backend mcp→legacy_direct→mcp, verifying each hop."""

    set_backend("execution_backend", "legacy_direct", path=backends_path)
    _append_run_event(
        run_dir,
        "rollback_flipped",
        key="execution_backend",
        **{"from": "mcp", "to": "legacy_direct"},
    )
    verified = load_backends(backends_path).execution_backend
    _append_run_event(run_dir, "rollback_verified", key="execution_backend", backend=verified)
    set_backend("execution_backend", "mcp", path=backends_path)
    _append_run_event(
        run_dir,
        "rollback_restored",
        key="execution_backend",
        **{"from": "legacy_direct", "to": "mcp"},
    )
    print(f"rollback leg: execution_backend mcp -> legacy_direct (verified={verified}) -> mcp")
    return BuildRollbackV1(
        checked=True,
        transitions=("mcp->legacy_direct", "legacy_direct->mcp"),
        legacy_verified_backend=verified,
    )


def _execution_facts(run_report: McpExecutionRunReportV1, episode_id: str) -> ExecutionFactsV1:
    rows = []
    for domain, actions in _DOMAIN_ACTIONS.items():
        steps = [s for s in run_report.steps if s.action in actions]
        if not steps:
            continue
        if any(s.status == "failed" for s in steps):
            outcome = "failed"
        elif any(s.rung == "manual_finalization" for s in steps):
            outcome = "manual_fallback"
        else:
            outcome = "executed"
        rows.append(DomainExecutionV1(domain=domain, outcome=outcome))
    return ExecutionFactsV1(episode_id=episode_id, domains=tuple(rows))


def _readback_matched(run_report: McpExecutionRunReportV1) -> int:
    return sum(
        1 for s in run_report.steps if s.attempts and s.attempts[-1].readback_matched is True
    )


def _load_json_model[M: BaseModel](path: Path, model: type[M]) -> M:
    return model.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _full_build_gate_evidence(run_dir: Path, *, dialogue_present: bool) -> FullBuildGateEvidenceV1:
    """Recompute the evidence from the run's ARTIFACTS, never the report."""

    ir = _load_json_model(run_dir / "timeline-ir-v2.json", TimelineIrV2)
    subtitle = _load_json_model(run_dir / "subtitle-plan.json", SubtitlePlanV1)
    run_report = _load_json_model(run_dir / "mcp-run-report.json", McpExecutionRunReportV1)
    quality = _load_json_model(run_dir / "quality-domain-report.json", QualityDomainReportV1)
    color_plan = _load_json_model(run_dir / "color-plan.json", ColorFinishingPlanV1)
    kit_artifact = _load_json_model(run_dir / "kit-selections.json", KitSelectionsArtifactV1)
    scaffold = _load_json_model(
        run_dir / "publishability-review-input.json", PublishabilityScaffoldV1
    )
    exec_plan_steps = json.loads((run_dir / "mcp-execution-plan.json").read_bytes())["steps"]
    events = [
        json.loads(line)
        for line in (run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    statuses = {entry.domain: entry.status for entry in quality.domains}
    technical = color_plan.technical_correction
    sections: tuple[tuple[str, bool], ...] = (
        (
            "technical_correction",
            technical.exposure.needed or technical.white_balance.needed,
        ),
        ("camera_shot_matching", color_plan.camera_shot_matching.needed),
        ("channel_episode_look", color_plan.channel_episode_look.needed),
    )
    return FullBuildGateEvidenceV1(
        plan_step_count=len(exec_plan_steps),
        subtitle_cue_count=len(subtitle.cues),
        dialogue_present=dialogue_present,
        audio_steps_executed=any(
            s.status == "completed" and s.action in _DOMAIN_ACTIONS["audio_finishing"]
            for s in run_report.steps
        ),
        color_steps_executed=any(
            s.status == "completed" and s.action in _DOMAIN_ACTIONS["color_finishing"]
            for s in run_report.steps
        ),
        color_sections_needed=tuple(name for name, needed in sections if needed),
        b_roll_track_present=any(t.role == "b_roll" for t in ir.video_tracks),
        primary_track_present=any(t.role == "primary" for t in ir.video_tracks),
        domain_statuses=statuses,
        blocked_domains=tuple(d for d, s in statuses.items() if s == "blocked"),
        recipe_selection_count=len(kit_artifact.selections),
        provenance_complete=all(
            sel.origin and sel.license and sel.fallback and sel.rationale
            for sel in kit_artifact.selections
        ),
        readback_matched_step_count=_readback_matched(run_report),
        run_outcome=run_report.outcome,
        render_record_present=(run_dir / "render-record.json").is_file(),
        publishability_scaffold_present=(run_dir / "publishability-review-input.json").is_file(),
        publishable=scaffold.publishable,
        rollback_transitions=tuple(
            f"{e['from']}->{e['to']}"
            for e in events
            if e.get("event") in ("rollback_flipped", "rollback_restored")
            and e.get("key") == "execution_backend"
        ),
    )


def _publishability_stage(
    inputs: _RunInputs, episode_id: str
) -> tuple[PublishabilityScaffoldV1, BuildPublishabilityV1]:
    path = inputs.run_dir / "publishability-review-input.json"
    if path.is_file():
        scaffold = _load_json_model(path, PublishabilityScaffoldV1)
    else:
        scaffold = PublishabilityScaffoldV1(
            schema_version="publishability-review-input-v1",
            episode_id=episode_id,
            run_id=inputs.run_id,
            note=(
                "operator fills publishable (as_is|after_small_corrections|not_yet); "
                "sparse input per PRD 14.4 — all other fields optional"
            ),
        )
        atomic_write(path, canonical_model_bytes(scaffold))
    return scaffold, BuildPublishabilityV1(
        scaffold=ArtifactFileRef(path=str(path), sha256=sha256_file(path)),
        publishable=scaffold.publishable,
        filled=scaffold.publishable is not None,
    )


@dataclass(frozen=True, slots=True)
class _PlansBundle:
    """The computed pre-execution plans (before any artifact write)."""

    episode_id: str
    intents: tuple[PresentationIntentV2, ...]
    density_within_limits: bool
    timeline_duration_seconds: float
    ir_final: TimelineIrV2
    subtitle_plan: SubtitlePlanV1
    audio_plan: AudioFinishingPlanV1
    color_plan: ColorFinishingPlanV1
    kit_selections: tuple[BuildKitSelectionV1, ...]
    exec_plan: McpExecutionPlanV1


@dataclass(frozen=True, slots=True)
class _PlanArtifactRefs:
    intents: ArtifactFileRef
    subtitle: ArtifactFileRef
    audio: ArtifactFileRef
    color: ArtifactFileRef
    plan: ArtifactFileRef


def _build_stage_plans(inputs: _RunInputs, editorial: _EditorialOutcome) -> _PlansBundle:
    episode_id = editorial.ir.episode_id
    profile = load_default_profile()
    intents = _author_presentation_intents(editorial.ir)
    density = check_density(
        intents, profile, timeline_duration_seconds=_timeline_seconds(editorial.ir)
    )
    ir_final = attach_presentation_intents(editorial.ir, intents)
    subtitle_plan = build_subtitle_plan(
        _asr_segments(editorial.artifact, inputs.rate),
        source_facts=editorial.source_facts,
        ir_v2=editorial.ir,
    )
    audio_plan = build_audio_plan(
        AudioFactsV1(
            episode_id=episode_id,
            dialogue_clean=False,
            has_bgm=any(t.role == "music" for t in ir_final.audio_tracks),
            has_ambience=any(t.role == "ambience" for t in ir_final.audio_tracks),
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        # synthetic fact dialogue_clean=False editorially justifies voice
        # isolation (the T38 fixture's proven request; capability accepted)
        op_requests=(AudioOpRequestV1(op="voice_isolation"),),
    )
    color_plan = build_color_plan(
        ColorFactsV1(
            episode_id=episode_id,
            exposure_issues=(
                ColorIssueV1(
                    source_id=editorial.artifact.sources[0].source_id,
                    detail="synthetic harness fact: underexposed intro (task-43 self-test)",
                ),
            ),
        ),
        policy=_color_policy(),
    )
    kit = load_kit()
    return _PlansBundle(
        episode_id=episode_id,
        intents=intents,
        density_within_limits=not density.violations,
        timeline_duration_seconds=density.timeline_duration_seconds,
        ir_final=ir_final,
        subtitle_plan=subtitle_plan,
        audio_plan=audio_plan,
        color_plan=color_plan,
        kit_selections=_kit_selections(
            kit, subtitle_plan=subtitle_plan, audio_plan=audio_plan, color_plan=color_plan
        ),
        exec_plan=compile_execution_plan(
            ir_final,
            subtitle_plan=subtitle_plan,
            audio_plan=audio_plan,
            color_plan=color_plan,
            presentation_intents=intents,
            kit_selections={intent.kind: select_recipe(kit, intent.kind) for intent in intents},
        ),
    )


def _write_plan_artifacts(run_dir: Path, bundle: _PlansBundle) -> _PlanArtifactRefs:
    episode_id = bundle.episode_id
    _artifact_ref(
        run_dir,
        "kit-selections.json",
        KitSelectionsArtifactV1(
            schema_version="kit-selections-v1",
            episode_id=episode_id,
            selections=bundle.kit_selections,
        ),
    )
    _artifact_ref(run_dir, "timeline-ir-v2.json", bundle.ir_final)
    return _PlanArtifactRefs(
        intents=_artifact_ref(
            run_dir,
            "presentation-intents.json",
            PresentationIntentsArtifactV1(
                schema_version="presentation-intents-v1",
                episode_id=episode_id,
                intents=bundle.intents,
                timeline_duration_seconds=bundle.timeline_duration_seconds,
                density_within_limits=bundle.density_within_limits,
            ),
        ),
        subtitle=_artifact_ref(run_dir, "subtitle-plan.json", bundle.subtitle_plan),
        audio=_artifact_ref(run_dir, "audio-plan.json", bundle.audio_plan),
        color=_artifact_ref(run_dir, "color-plan.json", bundle.color_plan),
        plan=_artifact_ref(run_dir, "mcp-execution-plan.json", bundle.exec_plan),
    )


def _run_full_build_stages(
    inputs: _RunInputs,
    *,
    backends_path: Path,
    executor_name: ExecutorName,
    executor: McpTransportFn | None,
) -> _FullBuildOutcome:
    editorial = _run_editorial_stages(inputs)
    run_dir = inputs.run_dir
    bundle = _build_stage_plans(inputs, editorial)
    episode_id = bundle.episode_id
    refs = _write_plan_artifacts(run_dir, bundle)
    intents = bundle.intents
    profile = load_default_profile()
    subtitle_plan = bundle.subtitle_plan
    audio_plan = bundle.audio_plan
    color_plan = bundle.color_plan
    kit_sel = bundle.kit_selections
    exec_plan = bundle.exec_plan
    ir_final = bundle.ir_final

    if executor_name == "fake":
        chosen: McpTransportFn = _SyntheticPlanExecutor(exec_plan)
    elif executor is not None:
        chosen = executor
    else:
        raise Episode0RerunError("executor-missing", "live executor was not probed")
    run_report = _execute_plan(
        exec_plan,
        run_dir=run_dir,
        backends_path=backends_path,
        executor=chosen,
        executor_name=executor_name,
    )
    run_ref = _artifact_ref(run_dir, "mcp-run-report.json", run_report)
    completed_steps = sum(1 for s in run_report.steps if s.status == "completed")
    render_ref = _artifact_ref(
        run_dir,
        "render-record.json",
        BuildRenderRecordV1(
            schema_version="synthetic-render-record-v1",
            executor=executor_name,
            episode_id=episode_id,
            run_outcome=run_report.outcome,
            completed_steps=completed_steps,
            note=(
                "fake executor: no real media rendered; planned render/QC steps "
                "completed with readback-verified synthetic actuals"
                if executor_name == "fake"
                else "live executor: render/QC steps completed via the pinned MCP server"
            ),
        ),
    )
    rollback = _rollback_leg(run_dir, backends_path)

    quality = build_domain_report(
        _quality_facts(editorial, intents, run_report),
        plans=QualityPlansV1(
            subtitle_plan=subtitle_plan,
            audio_plan=audio_plan,
            color_plan=color_plan,
        ),
        execution_report=_execution_facts(run_report, episode_id),
    )
    quality_ref = _artifact_ref(run_dir, "quality-domain-report.json", quality)
    qc_candidates = run_editorial_qc(
        EditorialQcInput(
            ir_v2=ir_final,
            subtitle_plan=subtitle_plan,
            selection=editorial.selection,
            creative_plan=editorial.creative_plan,
            story_plan=editorial.story_plan,
            presentation_intents=intents,
            presentation_profile=profile,
            audio_plan=audio_plan,
        )
    )
    qc_report = aggregate_candidates(qc_candidates)
    qc_ref = _artifact_ref(run_dir, "editorial-qc-report.json", qc_report)

    stub = McpExecutionPlanStub(
        schema_version="mcp-execution-plan-stub-v1",
        episode_id=episode_id,
        ir_sha256=hashlib.sha256(canonical_model_bytes(ir_final)).hexdigest(),
        steps=tuple(
            StubStepV2(step_id=s.step_id, capability=s.action, note=f"rung={s.rung}")
            for s in exec_plan.steps
        ),
    )
    preview_trace = render_presentation_preview(
        ir_final, stub, output_path=run_dir / "presentation-preview.trace.json"
    )
    preview_ref = _artifact_ref(run_dir, "presentation-preview.trace.json", preview_trace)
    _, publishability = _publishability_stage(inputs, episode_id)

    report = Episode0FullBuildReportV1(
        schema_version="episode0-full-build-report-v1",
        run_id=inputs.run_id,
        phase="full-build",
        episode_id=episode_id,
        executor=executor_name,
        inputs=editorial.report.inputs,
        editorial=editorial.report,
        plans=BuildPlansV1(
            presentation_intents=refs.intents,
            presentation_intent_count=len(intents),
            density_within_limits=bundle.density_within_limits,
            subtitle_plan=refs.subtitle,
            subtitle_cue_count=len(subtitle_plan.cues),
            audio_plan=refs.audio,
            color_plan=refs.color,
            kit_selections=kit_sel,
            execution_plan=refs.plan,
            plan_step_count=len(exec_plan.steps),
            plan_id=exec_plan.plan_id,
        ),
        execution=BuildExecutionV1(
            executor=executor_name,
            outcome=run_report.outcome,
            step_count=len(run_report.steps),
            completed_step_count=completed_steps,
            failed_step_count=sum(1 for s in run_report.steps if s.status == "failed"),
            readback_matched_step_count=_readback_matched(run_report),
            run_report=run_ref,
            render_record=render_ref,
        ),
        quality=BuildQualityV1(
            domain_report=quality_ref,
            domain_statuses={entry.domain: entry.status for entry in quality.domains},
            blocked_domains=tuple(
                entry.domain for entry in quality.domains if entry.status == "blocked"
            ),
            editorial_qc_report=qc_ref,
            qc_candidate_count=len(qc_report.candidates),
            qc_critical_count=qc_report.counts_by_severity["critical"],
            qc_needs_review_count=sum(1 for c in qc_candidates if c.needs_human_review),
        ),
        preview=BuildPreviewV1(trace=preview_ref, status=preview_trace.status),
        publishability=publishability,
        rollback=rollback,
    )
    evidence = _full_build_gate_evidence(
        run_dir, dialogue_present=bool(editorial.source_facts.transcripts)
    )
    return _FullBuildOutcome(
        report=report,
        editorial_evidence=editorial.evidence,
        gate=check_gate_v43_3(report, evidence),
    )


def _quality_facts(
    editorial: _EditorialOutcome,
    intents: tuple[PresentationIntentV2, ...],
    run_report: McpExecutionRunReportV1,
) -> QualityFactsV1:
    ir = editorial.ir
    primary_items = sum(len(t.items) for t in ir.video_tracks if t.role == "primary")
    graphic_items = sum(len(t.items) for t in ir.video_tracks if t.role in ("still", "graphic"))
    return QualityFactsV1(
        episode_id=ir.episode_id,
        has_dialogue=bool(editorial.source_facts.transcripts),
        editorial_blocking_defect=False,
        editorial_evidence=(
            f"moment-selection: {len(editorial.selection.candidates)} candidates committed",
            f"timeline-ir-v2: {primary_items} primary items",
        ),
        framing_motion_intended=bool(intents),
        framing_motion_evidence=tuple(f"presentation-intent: {i.intent_id}" for i in intents),
        graphics_intended=graphic_items > 0,
        graphics_evidence=(f"timeline-ir-v2: {graphic_items} graphic/still items",),
        delivery_qc_passed=run_report.outcome == "completed",
        delivery_qc_evidence=(
            f"mcp-execution-run-report-v1: outcome={run_report.outcome}",
            f"synthetic-render-record-v1: executor run, {run_report.outcome}",
        ),
    )


def _cmd_run_full_build(
    args: argparse.Namespace, inputs: _RunInputs, previous_contract: str
) -> int:
    backends_path = args.backends
    previous_exec = load_backends(backends_path).execution_backend
    executor_name = cast("ExecutorName", args.executor)
    executor: McpTransportFn | None = None
    if executor_name == "live":
        try:
            executor = _probe_live_executor(args.pin)
        except Episode0BlockedError as exc:
            print(f"blocked: {exc.code}: {exc.detail}", file=sys.stderr)
            print(
                "BLOCKED — live executor unavailable; escalated to operator. Not continuing.",
                file=sys.stderr,
            )
            return 1
    _note_prior_editorial_run(inputs)
    switched = False
    outcome: _FullBuildOutcome | None = None
    failure: BaseException | None = None
    try:
        set_backend("editorial_contract", "multimodal_v2", path=backends_path)
        _append_run_event(
            inputs.run_dir,
            "flag_switched",
            key="editorial_contract",
            **{"from": previous_contract, "to": "multimodal_v2"},
        )
        set_backend("execution_backend", "mcp", path=backends_path)
        _append_run_event(
            inputs.run_dir,
            "flag_switched",
            key="execution_backend",
            **{"from": previous_exec, "to": "mcp"},
        )
        switched = True
        print(
            f"editorial_contract: {previous_contract} -> multimodal_v2; "
            f"execution_backend: {previous_exec} -> mcp (backends={backends_path})"
        )
        outcome = _run_full_build_stages(
            inputs,
            backends_path=backends_path,
            executor_name=executor_name,
            executor=executor,
        )
    except Exception as exc:  # noqa: BLE001 (CLI boundary funnel: named failure, exit 1)
        print(f"run_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        failure = exc
    finally:
        if switched:
            try:
                set_backend("execution_backend", previous_exec, path=backends_path)
                _append_run_event(
                    inputs.run_dir,
                    "flag_restored",
                    key="execution_backend",
                    **{"from": "mcp", "to": previous_exec},
                )
                set_backend("editorial_contract", previous_contract, path=backends_path)
                _append_run_event(
                    inputs.run_dir,
                    "flag_restored",
                    key="editorial_contract",
                    **{"from": "multimodal_v2", "to": previous_contract},
                )
                print(
                    f"execution_backend: mcp -> {previous_exec}; "
                    f"editorial_contract: multimodal_v2 -> {previous_contract} (restored)"
                )
            except BackendsConfigError as exc:
                print(f"flag_restore_failed: {exc}", file=sys.stderr)
    if failure is not None or outcome is None:
        return 1
    report_path = inputs.run_dir / "report.json"
    atomic_write(report_path, canonical_model_bytes(outcome.report))
    editorial_gate = check_gate(outcome.report.editorial, outcome.editorial_evidence)
    atomic_write(inputs.run_dir / "gate-check.json", canonical_model_bytes(editorial_gate))
    gate_path = inputs.run_dir / "gate-check-v43-3.json"
    atomic_write(gate_path, canonical_model_bytes(outcome.gate))
    print(f"report: {report_path}")
    print(f"gate-check (V43-2, editorial stages): passed={editorial_gate.passed}")
    print(f"gate-check (V43-3, full build): {gate_path} passed={outcome.gate.passed}")
    return 0 if outcome.gate.passed else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "freeze-manifest":
        return _cmd_freeze_manifest(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "compare":
        return _cmd_compare(args)
    if args.command == "run":
        return _cmd_run(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
