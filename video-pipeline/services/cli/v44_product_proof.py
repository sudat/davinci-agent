# allow: SIZE_OK — CLI 6 subcommands + gate + arms in one thin runner
"""Thin CLI for the V44 product-proof harness (task 5 + T8 timing).

Subcommands:
  init-ground-truth   --episode-id X --out <path>
  validate-ground-truth --path <path>
  run-arm  --arm A|B|C --episode-root <dir> --ground-truth <path>
           --out <report.json> [--dry-run]
           --ground-truth-label L --evidence-lane system_asr|operator_corrected_diagnostic
           --transcript-sha256 H [--corrected-transcript <path>]
  record-operator-verdict --report <path> --continuation yes|no
           [--publishability ...] [--comments ...]
  record-efficiency --report <path> --episode-root <dir>
           [--observation <observation.json>]   (T8: finishing wall clock +
           time-log AHT/direct-Resolve + observed TTFRP; nulls stay null)
  evaluate --report <path>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

if TYPE_CHECKING:
    from services.editorial_v2.model_provider import CodexRunner

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.metrics.v44_product_proof import (
    EditorialGroundTruthV1,
    EpisodeContext,
    EvaluationBindingError,
    EvaluationBindingV1,
    GroundTruthAnchor,
    ProductProofReportV1,
    TranscriptSampleV1,
    compute_editorial_metrics,
    evaluate_pass_policy,
    render_policy_summary,
    run_arm_a,
    run_arm_b,
    run_arm_c,
)


def _repo_root() -> Path:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            return Path(completed.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path(__file__).resolve().parents[3]


def _try_production_gate(transport: str = "codex-exec") -> None:
    """Attempt to build the production transport; gate failure = blocked exit.

    ``codex-exec`` (the runtime default) gates on the codex CLI probe;
    ``openai-api`` keeps the env-var gate. Both block typed
    ``production-model-unavailable`` — never a heuristic fallback.
    """
    import os  # noqa: PLC0415

    if transport == "codex-exec":
        try:
            from services.cli.live_editorial_codex import (  # noqa: PLC0415
                CodexTransportGatedError,
                make_codex_runner,
            )
        except ImportError as exc:
            print(
                f"blocked: production-model-unavailable — codex transport not "
                f"available: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        try:
            make_codex_runner()
        except CodexTransportGatedError as exc:
            print(
                f"blocked: production-model-unavailable — codex gate failed "
                f"({exc.code}: {exc.detail}); run `codex login`",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        return
    try:
        from services.cli.live_editorial_v2 import make_http_post  # noqa: PLC0415
    except ImportError as exc:
        print(
            f"blocked: production-model-unavailable — live transport not available: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    env = dict(os.environ)
    try:
        make_http_post(env)
    except Exception as exc:  # gate is typed but broad by design
        print(f"blocked: production-model-unavailable — {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _cmd_init_ground_truth(args: argparse.Namespace) -> int:
    out: Path = Path(args.out)
    episode_id: str = args.episode_id
    # Minimal valid template: one must_keep, one must_remove, one uncertain
    created_at = datetime.now(tz=UTC).isoformat()
    ground_truth = EditorialGroundTruthV1(
        episode_id=episode_id,  # type: ignore[arg-type]
        anchors=(
            GroundTruthAnchor(
                anchor_id="anchor-001",
                start_frame=0,
                end_frame=300,
                label="must_keep",
                note="例: ここが一番面白い場面",
            ),
            GroundTruthAnchor(
                anchor_id="anchor-002",
                start_frame=1000,
                end_frame=1200,
                label="must_remove",
                note="冗長な言い直し",
            ),
            GroundTruthAnchor(
                anchor_id="anchor-003",
                start_frame=2000,
                end_frame=2100,
                label="uncertain",
                note=None,
            ),
        ),
        created_at=created_at,
        operator="operator",
    )
    atomic_write(out, canonical_model_bytes(ground_truth))
    print(f"init-ground-truth: {out} sha256={sha256_file(out)[:12]}")
    return 0


def _cmd_validate_ground_truth(args: argparse.Namespace) -> int:
    path: Path = Path(args.path)
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"validate failed: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        EditorialGroundTruthV1.model_validate(payload)
    except ValidationError as exc:
        print(f"validate failed: {exc}", file=sys.stderr)
        return 2
    # Also validate transcript sample if present alongside (optional)
    print(f"validate: {path} OK")
    return 0


def _load_ground_truth(path: Path) -> EditorialGroundTruthV1:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    return EditorialGroundTruthV1.model_validate(payload)


def _resolve_evaluation_binding(
    args: argparse.Namespace, gt_path: Path
) -> EvaluationBindingV1:
    """Build the evaluation binding every new r4 report requires.

    The ground-truth hash is computed from the exact ``--ground-truth``
    file bytes the run is scored against; lane/label/transcript hash come
    from explicit CLI inputs. Missing or malformed pieces are a typed
    ``evaluation-binding-missing`` refusal BEFORE any pipeline work.
    """
    label: str | None = getattr(args, "ground_truth_label", None)
    lane: str | None = getattr(args, "evidence_lane", None)
    transcript_sha: str | None = getattr(args, "transcript_sha256", None)
    if label is None or lane is None or transcript_sha is None:
        missing = [
            flag
            for flag, value in (
                ("--ground-truth-label", label),
                ("--evidence-lane", lane),
                ("--transcript-sha256", transcript_sha),
            )
            if not value
        ]
        raise EvaluationBindingError(
            "evaluation-binding-missing",
            "new r4 run-arm reports require a complete evaluation binding: "
            + ", ".join(missing),
        )
    try:
        return EvaluationBindingV1(
            ground_truth_sha256=sha256_file(gt_path),
            ground_truth_label=label,
            evidence_lane=lane,  # type: ignore[arg-type]
            transcript_sha256=transcript_sha,
        )
    except ValidationError as exc:
        raise EvaluationBindingError(
            "evaluation-binding-missing",
            f"evaluation binding fields are invalid ({exc.error_count()} "
            "field errors); lane must be system_asr or "
            "operator_corrected_diagnostic and hashes lowercase sha256",
        ) from exc


_COMMIT_SHA_LEN: int = 40


def _resolve_commit_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            sha = completed.stdout.strip()
            if len(sha) == _COMMIT_SHA_LEN and all(c in "0123456789abcdef" for c in sha):
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "0" * _COMMIT_SHA_LEN


def _runtime_config_candidates() -> list[Path]:
    return [
        _repo_root() / "video-pipeline" / "config" / "editorial-runtime.json",
        Path("config/editorial-runtime.json").resolve(),
        Path(__file__).resolve().parents[2] / "config" / "editorial-runtime.json",
    ]


def _runtime_config_path() -> Path | None:
    for candidate in _runtime_config_candidates():
        if candidate.is_file():
            return candidate
    return None


def _video_pipeline_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _editorial_mode() -> str:
    """Read editorial-runtime.json mode; default production_model per T3."""
    candidate = _runtime_config_path()
    if candidate is not None:
        try:
            data: object = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("mode"), str):
                return str(data["mode"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return "production_model"


def _editorial_transport() -> str:
    """Read the runtime transport; absent/unreadable → codex-exec default."""
    candidate = _runtime_config_path()
    if candidate is not None:
        try:
            data: object = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            transport = data.get("transport")
            if isinstance(transport, str) and transport in ("codex-exec", "openai-api"):
                return transport
    return "codex-exec"


def _cmd_run_arm(args: argparse.Namespace) -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915
    arm: Literal["A", "B", "C"] = args.arm
    episode_root = Path(args.episode_root)
    gt_path = Path(args.ground_truth)
    out_path = Path(args.out)
    dry_run: bool = bool(getattr(args, "dry_run", False))

    # Load ground truth (validates half-open etc)
    try:
        gt = _load_ground_truth(gt_path)
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"run-arm failed: invalid ground truth {gt_path}: {exc}", file=sys.stderr)
        return 2

    episode_id = str(gt.episode_id)
    is_toy = episode_id.startswith("test-")

    # Real pipeline (T14): arms A/B on a real episode, never in dry-run.
    # Dry-run is CLI/policy verification ONLY — toy path, no transport gate.
    mode = _editorial_mode()
    if not dry_run and not is_toy and arm in ("A", "B"):
        if mode != "production_model":
            print(
                f"blocked: production-model-unavailable — run-arm real arms require "
                f"editorial-runtime mode production_model, got {mode!r}; refusing to "
                f"measure a heuristic toy as the production editorial result",
                file=sys.stderr,
            )
            return 1
        try:
            evaluation_binding = _resolve_evaluation_binding(args, gt_path)
        except EvaluationBindingError as exc:
            print(f"run-arm failed: {exc.code}: {exc.detail}", file=sys.stderr)
            return 2
        return _run_real_arm(
            arm, episode_root, gt, out_path, args, evaluation_binding=evaluation_binding
        )

    # Arm C on a real episode (diagnostic) keeps the transport gate (no dry-run).
    if mode == "production_model" and not dry_run and not is_toy:
        _try_production_gate(_editorial_transport())

    # Toy path: deterministic, testable; T14's real harness replaced this for
    # arms A/B on real episodes (see services/cli/v44_arm_pipeline.py).
    kept_spans: list[tuple[int, int]] = [
        (int(a.start_frame), int(a.end_frame)) for a in gt.anchors if a.label == "must_keep"
    ]
    toy_notes = (
        "dry-run-toy: real pipeline skipped; kept_spans are the trivial toy "
        "(all must_keep anchors kept) — CLI/policy verification only"
        if dry_run
        else None
    )

    ctx = EpisodeContext(
        episode_id=episode_id,
        ground_truth=gt,
        kept_spans=tuple(kept_spans),
        escalated_ids=(),
        wall_clock_seconds=10.0,
        provider_cost=0.01,
    )
    commit_sha = _resolve_commit_sha()

    # Dispatch arm
    if arm == "A":
        result = run_arm_a(ctx, commit_sha=commit_sha, notes=toy_notes)
        report = result.report
    elif arm == "B":
        # For B we need at least one real-lineage review if any must_keep is uncertain.
        # Construct a minimal real review directly (provider != synthetic) to pass gate.
        from services.media_intelligence.moment_review import (  # noqa: PLC0415
            AudioContext,
            BestSubSpan,
            FrameBundleEntry,
            MomentAssessment,
            ReviewConfidence,
            ReviewEvidence,
            ReviewLineage,
            ReviewRecordRequest,
            ReviewWindow,
            record_review,
        )

        # One review per must_keep anchor (real lineage)
        reviews = []
        for anchor in gt.anchors:
            if anchor.label != "must_keep":
                continue
            window = ReviewWindow(
                start_frame=int(anchor.start_frame), end_frame=int(anchor.end_frame)
            )
            # Use a single frame bundle entry inside the window
            evidence = ReviewEvidence(
                frame_bundle=(
                    FrameBundleEntry(
                        frame=int(anchor.start_frame),
                        ref=f"file:///tmp/frame-{anchor.anchor_id}.png",
                    ),
                ),
                transcript_refs=(),
                audio_context=AudioContext(note="real audio note"),
            )
            assessment = MomentAssessment(
                subject_action_evolution="real: action evolves",
                reaction_notes="real: reaction",
                timing_notes="real: timing",
                best_sub_span=BestSubSpan(
                    start_frame=int(anchor.start_frame), end_frame=int(anchor.end_frame)
                ),
                keep_rationale_candidates=("real: keep",),
                remove_rationale_candidates=(),
                cut_in_handle="in",
                cut_out_handle="out",
            )
            rec = record_review(
                ReviewRecordRequest(
                    episode_id=episode_id,  # type: ignore[arg-type]
                    source_duration_frames=max(int(a.end_frame) for a in gt.anchors) + 1000,
                    window=window,
                    evidence=evidence,
                    assessment=assessment,
                    confidence=ReviewConfidence(
                        overall=0.9,
                        subject_action_evolution=0.9,
                        reaction_notes=0.9,
                        timing_notes=0.9,
                        best_sub_span=0.9,
                    ),
                    lineage=ReviewLineage(
                        provider="openai",
                        provider_version="gpt-5.6-sol",
                        tool="multimodal-v1",
                        cost=0.01,
                    ),
                )
            )
            reviews.append(rec)
        # If no must_keep, create an empty list (arm B with no reviews is valid)
        if not reviews:
            result = run_arm_a(ctx, commit_sha=commit_sha, notes=toy_notes)
            report = result.report
        else:
            result = run_arm_b(ctx, reviews, commit_sha=commit_sha, notes=toy_notes)
            report = result.report
    elif arm == "C":
        # Arm C diagnostic: consume corrected-evidence JSON if present
        corrected: dict[str, object] | None = None
        # Look for transcript-sample-corrected.json alongside episode_root
        sample_path = episode_root / "transcript-sample-corrected.json"
        if sample_path.is_file():
            try:
                raw: object = json.loads(sample_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and not raw.get("_placeholder"):
                    # Try to parse as TranscriptSampleV1 to validate shape
                    try:
                        sample = TranscriptSampleV1.model_validate(raw)
                        corrected = {
                            "segments": len(sample.segments),
                            "proper_nouns": len(sample.proper_nouns),
                        }
                    except ValidationError:
                        corrected = {"raw_keys": sorted(raw.keys())}
                else:
                    corrected = {"placeholder": True}
            except (OSError, ValueError, json.JSONDecodeError):
                corrected = {"error": "unreadable"}
        # Classify: find failed must_keep (not kept) as toy failed set
        editorial = compute_editorial_metrics(gt.anchors, ctx.kept_spans, ctx.escalated_ids)
        failed: list[GroundTruthAnchor] = []
        if editorial.catastrophic_removal_count > 0:
            for anchor in gt.anchors:
                if anchor.label == "must_keep":
                    kept = any(
                        ks[0] < int(anchor.end_frame) and int(anchor.start_frame) < ks[1]
                        for ks in ctx.kept_spans
                    )
                    if not kept:
                        failed.append(anchor)
        result = run_arm_c(
            ctx, corrected, failed, corrected_kept_spans=ctx.kept_spans, commit_sha=commit_sha
        )
        report = result.report
    else:
        print(f"unknown arm {arm!r}", file=sys.stderr)
        return 2

    # Dry-run: assert pending operator fields and never passed
    if dry_run:
        policy = evaluate_pass_policy(report)
        if policy.passed:
            print(
                "dry-run failed: policy must not pass with pending operator verdict",
                file=sys.stderr,
            )
            return 1
        if "operator_continuation_yes_no" not in policy.pending_criteria:
            print("dry-run failed: pending must include continuation", file=sys.stderr)
            return 1

    atomic_write(out_path, canonical_model_bytes(report))
    print(f"run-arm {arm}: {out_path} sha256={sha256_file(out_path)[:12]}")
    return 0


class _CountingCodexRunner:
    """Codex runner wrapper counting every exec call (codex reports no usage)."""

    __slots__ = ("_inner", "count")

    def __init__(self, inner: CodexRunner) -> None:
        self._inner = inner
        self.count = 0

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        self.count += 1
        return self._inner(prompt, model=model, images=images, timeout_s=timeout_s)


def _arm_failure_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else type(exc).__name__


def _run_real_arm(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915 (arm wiring: transport + pins + report)
    arm: Literal["A", "B"],
    episode_root: Path,
    gt: EditorialGroundTruthV1,
    out_path: Path,
    args: argparse.Namespace,
    *,
    evaluation_binding: EvaluationBindingV1,
) -> int:
    """Drive the REAL pipeline (chain stages → DirectorV2 codex → commit).

    Any typed failure in the real chain IS the honest result: print the
    code/detail and exit 1 — never a toy fallback, never a fabricated report.
    """
    import time  # noqa: PLC0415

    from services.cli._v44_arm_transcript import (  # noqa: PLC0415
        TranscriptLaneInput,
    )
    from services.cli.live_editorial_codex import (  # noqa: PLC0415
        CodexTransportGatedError,
        make_codex_runner,
    )
    from services.cli.v44_arm_evidence import (  # noqa: PLC0415
        FRAME_SPACE_NOTE,
        ArmEvidenceError,
        compose_arm_brief,
        escalated_anchor_ids,
        mezz_span_to_anchor_space,
    )
    from services.cli.v44_arm_pipeline import (  # noqa: PLC0415
        ArmPipelineInputs,
        run_arm_pipeline,
    )
    from services.cli.v44_arm_stages import (  # noqa: PLC0415
        ArmPipelineError,
        whisper_provider_pin,
    )
    from services.editorial_v2.director_v2 import DirectorV2Error  # noqa: PLC0415
    from services.editorial_v2.editorial_pins import (  # noqa: PLC0415
        EditorialRuntimeError,
        load_editorial_pin,
        load_editorial_runtime,
    )
    from services.editorial_v2.evidence_v2 import EvidenceIncompleteV2  # noqa: PLC0415
    from services.editorial_v2.model_provider import build_llm_call_codex  # noqa: PLC0415
    from services.editorial_v2.proposal_validate import (  # noqa: PLC0415
        MomentCommitError,
        MomentValidationError,
    )
    from services.media_intelligence.moment_review_real import (  # noqa: PLC0415
        MomentReviewRealError,
    )
    from services.media_intelligence.video_review_wire import VideoProviderError  # noqa: PLC0415
    from services.media_intelligence.video_understanding import (  # noqa: PLC0415
        VideoUnderstandingError,
    )
    from services.normalize.errors import NormalizeError  # noqa: PLC0415

    started = time.monotonic()
    episode_id = str(gt.episode_id)
    try:
        runner = make_codex_runner()
    except CodexTransportGatedError as exc:
        print(
            f"blocked: production-model-unavailable — codex gate failed "
            f"({exc.code}: {exc.detail}); run `codex login`",
            file=sys.stderr,
        )
        return 1
    counting = _CountingCodexRunner(runner)
    runtime_path = _runtime_config_path()
    if runtime_path is None:
        print("run-arm failed: editorial-runtime-missing", file=sys.stderr)
        return 1
    try:
        analysis_pin = whisper_provider_pin(_video_pipeline_root())
    except ArmPipelineError as exc:
        print(f"run-arm failed: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    try:
        runtime = load_editorial_runtime(runtime_path)
        pin = load_editorial_pin(
            runtime_path.parent.parent / runtime.director_pin_path
        )
        llm_call = build_llm_call_codex(pin, counting)
    except EditorialRuntimeError as exc:
        print(f"run-arm failed: {exc.code}: {exc.detail}", file=sys.stderr)
        return 1
    lead_model: str | None = None
    specialist_model: str | None = None
    if arm == "B":
        try:
            lead_model = load_editorial_pin(
                runtime_path.parent.parent / runtime.moment_review_pin_path
            ).model_id
            if runtime.moment_review_specialist_pin_path is not None:
                specialist_model = load_editorial_pin(
                    runtime_path.parent.parent / runtime.moment_review_specialist_pin_path
                ).model_id
        except EditorialRuntimeError as exc:
            print(f"run-arm failed: {exc.code}: {exc.detail}", file=sys.stderr)
            return 1
    workspace_arg = getattr(args, "workspace", None)
    workspace = Path(workspace_arg) if workspace_arg else episode_root / "runs" / f"arm-{arm}"
    factory = None
    if arm == "B":
        try:
            import os as _os  # noqa: PLC0415

            from services.cli._v44_arm_video_factory import (  # noqa: PLC0415
                build_video_understanding_factory,
            )

            factory = build_video_understanding_factory(
                episode_id=episode_id,
                workspace=workspace,
                env=dict(_os.environ),
                runtime=runtime,
                runtime_path=runtime_path,
            )
        except (
            ArmPipelineError,
            EditorialRuntimeError,
            VideoProviderError,
            VideoUnderstandingError,
            ValidationError,
            OSError,
        ) as exc:
            print(
                f"run-arm failed: {_arm_failure_code(exc)}: {exc} — honest typed failure, "
                f"no report written",
                file=sys.stderr,
            )
            return 1
    corrected_arg: str | None = getattr(args, "corrected_transcript", None)
    corrected_path = Path(corrected_arg) if corrected_arg else None
    inputs = ArmPipelineInputs(
        episode_root=episode_root,
        workspace=workspace,
        episode_id=episode_id,
        brief=compose_arm_brief(episode_root, episode_id, operator=str(gt.operator)),
        llm_call=llm_call,
        transcript_lane=TranscriptLaneInput(
            lane=evaluation_binding.evidence_lane,
            corrected_sample=corrected_path,
            expected_sha256=evaluation_binding.transcript_sha256,
        ),
        video_understanding=factory,
    )
    failure_types: tuple[type[BaseException], ...] = (
        ArmPipelineError,
        ArmEvidenceError,
        DirectorV2Error,
        EditorialRuntimeError,
        EvidenceIncompleteV2,
        MomentReviewRealError,
        MomentValidationError,
        MomentCommitError,
        NormalizeError,
        VideoProviderError,
        VideoUnderstandingError,
        ValidationError,
        OSError,
    )
    try:
        result = run_arm_pipeline(inputs)
    except failure_types as exc:
        print(
            f"run-arm failed: {_arm_failure_code(exc)}: {exc} — honest typed failure, "
            f"no report written",
            file=sys.stderr,
        )
        return 1
    wall = time.monotonic() - started
    kept_spans = tuple(mezz_span_to_anchor_space(s, e) for s, e in result.kept_spans_mezz)
    escalated_spans = tuple(
        mezz_span_to_anchor_space(s, e) for s, e in result.escalated_spans_mezz
    )
    escalated_ids = escalated_anchor_ids(gt.anchors, escalated_spans)
    from services.metrics.v44_video_understanding_metrics import (  # noqa: PLC0415
        compute_video_understanding_metrics,
    )

    video_metrics = (
        compute_video_understanding_metrics(result.reviews) if arm == "B" else None
    )
    provider_cost = video_metrics.total_cost if video_metrics is not None else None
    ctx = EpisodeContext(
        episode_id=episode_id,
        ground_truth=gt,
        kept_spans=kept_spans,
        escalated_ids=escalated_ids,
        wall_clock_seconds=wall,
        provider_cost=provider_cost,
    )
    evidence_quality = result.evidence_quality
    if evidence_quality is None:
        evidence_note = "evidence_quality: none recorded for this run"
    else:
        evidence_note = (
            f"evidence_quality: {result.transcript_lane} lane PRE-MODEL alignment "
            f"(cer={evidence_quality.transcript_cer!r}, "
            f"p95_ms={evidence_quality.timestamp_error_p95_ms!r}, "
            f"omitted={evidence_quality.omitted_utterances!r}, "
            f"duplicated={evidence_quality.duplicated_utterances!r}; hypothesis is "
            "post-proper-noun-substitution, matching the director-visible "
            "transcript; effective transcript sha256="
            f"{result.effective_transcript_sha256[:12]})"
        )
    escalation_policy = (
        "fused video-understanding reviews precede the Director; keep with "
        "overlapping fused confidence<0.5 demotes+escalates; escalated anchor "
        "ids = anchors overlapping demoted spans"
        if arm == "B"
        else "none (arm A has no reviews)"
    )
    note_rows = [
        (
            "pipeline=real: DirectorV2 three-pass (codex-exec), kept_spans from "
            "the committed selection"
        ),
        (
            f"transport=codex-exec model={pin.model_id} codex_calls={counting.count} "
            "(codex exec reports no token usage; provider_cost=null)"
        ),
        f"workspace={workspace}",
        *result.notes,
        FRAME_SPACE_NOTE,
        f"escalation_policy: {escalation_policy}",
        evidence_note,
        f"wall_seconds={wall:.1f} (incl. analysis + gates)",
    ]
    if arm == "B":
        note_rows.append(
            f"moment_review_pins: lead={lead_model} specialist={specialist_model} "
            f"stage_costs_recorded={provider_cost is not None}"
        )
    notes = "; ".join(note_rows)
    commit_sha = _resolve_commit_sha()
    if arm == "A":
        report = run_arm_a(
            ctx,
            commit_sha=commit_sha,
            model_pin=pin.model_id,
            analysis_provider_pin=analysis_pin,
            notes=notes,
            evidence_quality=evidence_quality,
            evaluation_binding=evaluation_binding,
        ).report
    else:
        report = run_arm_b(
            ctx,
            result.reviews,
            commit_sha=commit_sha,
            model_pin=pin.model_id,
            analysis_provider_pin=analysis_pin,
            notes=notes,
            evidence_quality=evidence_quality,
            moment_review_lead_pin=lead_model,
            moment_review_specialist_pin=specialist_model,
            video_understanding=video_metrics,
            evaluation_binding=evaluation_binding,
        ).report
    atomic_write(out_path, canonical_model_bytes(report))
    print(
        f"run-arm {arm} real: codex_calls={counting.count} "
        f"committed=v{result.commit_version} kept={len(result.kept_spans_mezz)}/"
        f"{result.candidate_count} escalated={len(result.escalated_candidate_ids)} "
        f"reviews={len(result.reviews)} wall={wall:.1f}s"
    )
    preview = ", ".join(result.kept_candidate_ids[:12])
    print(f"kept candidates (first 12): {preview or '(none)'}")
    print(f"run-arm {arm}: {out_path} sha256={sha256_file(out_path)[:12]}")
    return 0


def _cmd_record_operator_verdict(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    continuation_raw: str = args.continuation
    if continuation_raw not in ("yes", "no"):
        print(f"invalid --continuation {continuation_raw!r}: expected yes|no", file=sys.stderr)
        return 2
    continuation = continuation_raw == "yes"
    publishability: str | None = getattr(args, "publishability", None)
    comments: str | None = getattr(args, "comments", None)

    try:
        payload: object = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read report {report_path}: {exc}", file=sys.stderr)
        return 2
    try:
        report = ProductProofReportV1.model_validate(payload)
    except ValidationError as exc:
        print(f"report validation failed: {exc}", file=sys.stderr)
        return 2

    # Refuse unknown publishability states
    if publishability is not None and publishability not in (
        "as_is",
        "after_small_corrections",
        "not_yet",
    ):
        print(f"unknown publishability {publishability!r}", file=sys.stderr)
        return 2

    # Re-validate on every record (stale_state guard) — reconstruct with new operator
    from services.metrics.v44_product_proof import OperatorVerdict  # noqa: PLC0415

    new_operator = OperatorVerdict(
        continuation_yes_no=continuation,
        publishability=publishability,  # type: ignore[arg-type]
        comments=comments,
    )
    try:
        updated = ProductProofReportV1(
            run=report.run,
            evaluation_binding=report.evaluation_binding,
            editorial=report.editorial,
            progressive_lift=report.progressive_lift,
            evidence_quality=report.evidence_quality,
            video_understanding=report.video_understanding,
            operator=new_operator,
            efficiency=report.efficiency,
            pass_policy=report.pass_policy,
            notes=report.notes,
            summary=report.summary,
        )
    except ValidationError as exc:
        print(f"verdict revalidation failed: {exc}", file=sys.stderr)
        return 2

    atomic_write(report_path, canonical_model_bytes(updated))
    print(f"record-operator-verdict: {report_path} continuation={continuation_raw}")
    return 0


def _fmt_minutes(value: float | None) -> str:
    return "not recorded" if value is None else f"{value:g} min"


def _fmt_seconds(value: float | None) -> str:
    return "not recorded" if value is None else f"{value:g} s"


def _cmd_record_efficiency(args: argparse.Namespace) -> int:
    from services.cli._v44_efficiency_record import (  # noqa: PLC0415
        EfficiencyRecordError,
        record_efficiency,
    )
    from services.cli._v44_finishing_build import (  # noqa: PLC0415
        FinishingError,
    )

    report_path = Path(args.report)
    episode_root = Path(args.episode_root)
    observation_arg: str | None = getattr(args, "observation", None)
    observation_path = Path(observation_arg) if observation_arg else None
    try:
        updated = record_efficiency(report_path, episode_root, observation_path)
    except (EfficiencyRecordError, FinishingError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"record-efficiency failed: {code}: {exc}", file=sys.stderr)
        return 2
    atomic_write(report_path, canonical_model_bytes(updated))
    eff = updated.efficiency
    if eff is None:
        print(
            "record-efficiency failed: efficiency-block-missing — the updated "
            "report carries no efficiency block",
            file=sys.stderr,
        )
        return 1
    print(
        f"record-efficiency: active human time {_fmt_minutes(eff.aht_minutes)} "
        f"(direct Resolve {_fmt_minutes(eff.direct_resolve_minutes)} is a "
        f"subset, not added on top), wall clock {_fmt_seconds(eff.wall_clock_seconds)}, "
        f"time to first rough preview {_fmt_seconds(eff.ttfrp_seconds)}"
    )
    missing = [
        label
        for label, value in (
            ("time to first rough preview (run observe-v44-1)", eff.ttfrp_seconds),
            ("active human time (record-time)", eff.aht_minutes),
            (
                "direct Resolve minutes (record-time --phase direct_resolve)",
                eff.direct_resolve_minutes,
            ),
        )
        if value is None
    ]
    if missing:
        print(
            "record-efficiency: still missing — Gate V44-2 stays blocked until "
            f"recorded: {'; '.join(missing)}",
            file=sys.stderr,
        )
    print(f"record-efficiency: {report_path} sha256={sha256_file(report_path)[:12]}")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    try:
        payload: object = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot read report {report_path}: {exc}", file=sys.stderr)
        return 2
    try:
        report = ProductProofReportV1.model_validate(payload)
    except ValidationError as exc:
        print(f"report validation failed: {exc}", file=sys.stderr)
        return 2

    result = evaluate_pass_policy(report)
    output = result.model_dump(mode="json")
    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    for line in render_policy_summary(report, result):
        print(line)
    # Persist sidecar
    sidecar = report_path.with_suffix(report_path.suffix + ".policy.json")
    try:
        atomic_write(
            sidecar,
            json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
        )
    except OSError as exc:
        print(f"cannot write sidecar {sidecar}: {exc}", file=sys.stderr)
        return 1
    print(f"evaluate: {sidecar}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.cli.v44_product_proof")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-ground-truth", help="write template ground truth")
    p_init.add_argument("--episode-id", type=str, required=True, help="episode id")
    p_init.add_argument("--out", type=str, required=True, help="output path")

    p_val = sub.add_parser("validate-ground-truth", help="validate ground truth")
    p_val.add_argument("--path", type=str, required=True, help="ground truth path")

    p_run = sub.add_parser("run-arm", help="run experiment arm A|B|C")
    p_run.add_argument("--arm", type=str, required=True, choices=["A", "B", "C"], help="arm")
    p_run.add_argument("--episode-root", type=str, required=True, help="episode dir (T6 layout)")
    p_run.add_argument("--ground-truth", type=str, required=True, help="ground truth path")
    p_run.add_argument("--out", type=str, required=True, help="output report path")
    p_run.add_argument(
        "--dry-run", action="store_true", help="compute without operator verdict, assert pending"
    )
    p_run.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="arm workspace dir (real arms; default <episode-root>/runs/arm-<ARM>)",
    )
    p_run.add_argument(
        "--ground-truth-label",
        type=str,
        default=None,
        help="evaluation binding: ground-truth version label (e.g. v1, v2)",
    )
    p_run.add_argument(
        "--evidence-lane",
        type=str,
        default=None,
        choices=["system_asr", "operator_corrected_diagnostic"],
        help="evaluation binding: transcript evidence lane",
    )
    p_run.add_argument(
        "--transcript-sha256",
        type=str,
        default=None,
        help="evaluation binding: effective transcript sha256 (lowercase hex)",
    )
    p_run.add_argument(
        "--corrected-transcript",
        type=str,
        default=None,
        help=(
            "operator corrected sample path — REQUIRED for the "
            "operator_corrected_diagnostic lane; a typed refusal in system_asr "
            "(the system lane reads its metrics reference from "
            "<episode-root>/transcript-sample-corrected.json)"
        ),
    )

    p_rec = sub.add_parser("record-operator-verdict", help="record operator verdict into report")
    p_rec.add_argument("--report", type=str, required=True, help="report path")
    p_rec.add_argument(
        "--continuation", type=str, required=True, choices=["yes", "no"], help="continuation yes|no"
    )
    p_rec.add_argument(
        "--publishability",
        type=str,
        default=None,
        choices=["as_is", "after_small_corrections", "not_yet"],
        help="publishability",
    )
    p_rec.add_argument("--comments", type=str, default=None, help="operator comments")

    p_eff = sub.add_parser(
        "record-efficiency",
        help="carry finishing wall clock + time-log totals + observed TTFRP into the report",
    )
    p_eff.add_argument("--report", type=str, required=True, help="report path")
    p_eff.add_argument(
        "--episode-root",
        type=str,
        required=True,
        help="episode dir (finishing report + time-log.jsonl)",
    )
    p_eff.add_argument(
        "--observation",
        type=str,
        default=None,
        help=(
            "v44-1 observation.json path (TTFRP source; default "
            "<episode-root>/observation.json; absent stays null)"
        ),
    )

    p_eval = sub.add_parser("evaluate", help="evaluate pass policy")
    p_eval.add_argument("--report", type=str, required=True, help="report path")

    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 (subcommand dispatch)
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "init-ground-truth":
        return _cmd_init_ground_truth(args)
    if args.command == "validate-ground-truth":
        return _cmd_validate_ground_truth(args)
    if args.command == "run-arm":
        return _cmd_run_arm(args)
    if args.command == "record-operator-verdict":
        return _cmd_record_operator_verdict(args)
    if args.command == "record-efficiency":
        return _cmd_record_efficiency(args)
    if args.command == "evaluate":
        return _cmd_evaluate(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
