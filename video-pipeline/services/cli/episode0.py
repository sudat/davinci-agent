"""``python -m services.cli.episode0`` — Episode-0 baseline + phase rerun tooling.

Subcommands:
  freeze-manifest — hash/size/duration for real-01 source (from episode.json)
  report          — operator log + manifest → runs/<run-id>/report.json
  compare         — delta between two reports (file paths or run labels)
  run             — phase rerun harness: ``run --phase editorial-v2`` executes
                    brief → media-intelligence v2 → Director v2 three-pass
                    (llm_call=None) → validation+commit → synthetic Review
                    Event correction → Timeline IR v2 → Editorial Preview →
                    measurement report + Gate V43-2 checklist.
"""

# allow: SIZE_OK — task 30 pins the rerun-harness commit scope to this T5 CLI
# module plus metrics/episode0_gate_v43_2.py (all contracts live there). The
# added code is one responsibility — rerun orchestration — and each stage is a
# thin delegate to an existing service. T29/T31 single-module precedent.

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from services.config.backends import BackendsConfigError, load_backends, set_backend
from services.contracts.primitives import RationalFrameRate
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
from services.preview.errors import PreviewError
from services.preview.tools import load_pinned_tools
from services.preview.v2_editorial import render_editorial_preview
from services.preview.v2_models import SourceMediaEntryV2, SourceMediaMapV2
from services.reference_learning.models import DerivedTasteProfileV1
from services.review_command.events import MOMENT_SELECTION_V2_COMMITTED
from services.review_command.store import load_events

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.editorial_v2.director_v2 import ThreePassResult
    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.proposal_validate import CommitReceipt, ValidationResult

DEFAULT_EPISODE_JSON = Path("private/reference-episodes/real-01/episode.json")
DEFAULT_RUNS_ROOT = Path("private/reference-episodes/real-01/runs")
DEFAULT_BACKENDS = Path("config/backends.json")
_MIN_KEEPS_FOR_CORRECTION = 2
PHASE_0C_LOCK = Path("config/toolchains/phase-0c-v1.json")


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
    run.add_argument("--phase", required=True, choices=["editorial-v2"])
    run.add_argument("--episode", required=True, help="reference episode id (e.g. real-01)")
    run.add_argument("--episode-root", type=Path, default=None)
    run.add_argument("--runs-root", type=Path, default=None)
    run.add_argument("--brief", type=Path, required=True, help="approved episode-brief-v1 json")
    run.add_argument("--mi-artifact", type=Path, required=True, help="media-intelligence-v2 json")
    run.add_argument("--taste-profile", type=Path, default=None, help="taste profile json")
    run.add_argument("--source-media", type=Path, default=None, help="preview edit-source media")
    run.add_argument("--frame-rate", type=str, default="30/1", help="edit-source rate as num/den")
    run.add_argument("--backends", type=Path, default=DEFAULT_BACKENDS)
    run.add_argument("--run-id", type=str, default=None, help="default <date>-editorial-v2")

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


def _stage_compile(
    deps: _StageDeps, result: ThreePassResult, proposal: MomentSelectionProposalV2
) -> tuple[CreativeEditPlanProposalV2, TimelineIrV2]:
    if len(deps.artifact.sources) != 1:
        raise Episode0RerunError(
            "multi-source-unsupported",
            f"editorial-v2 rerun harness supports single-source episodes, got "
            f"{len(deps.artifact.sources)} sources",
        )
    source = deps.artifact.sources[0]
    assert source.duration_frames is not None
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
    return plan, compile_ir_v2(proposal, plan, source_facts=facts)


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


def _run_editorial_v2(
    inputs: _RunInputs,
) -> tuple[Episode0RerunReportV1, GateEvidenceV1]:
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
        plan, ir = _stage_compile(deps, result, corrected)
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
    return report, evidence


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
    run_id = args.run_id or f"{datetime.now(tz=UTC).date().isoformat()}-editorial-v2"
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
        report, evidence = _run_editorial_v2(inputs)
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
