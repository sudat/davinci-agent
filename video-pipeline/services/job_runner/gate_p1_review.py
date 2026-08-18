"""The declared review-correction sequence driver (Phase-1 gate).

Runs each declared review command through the REAL Todo-45 translator
(``propose_review_command`` over the live bundle head) and apply engine
(``apply_proposal``), recording raw per-command outcomes, plus the
non-declared schema-gap probe that must be refused typed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli.bundle import BUNDLE_NAME, load_bundle
from services.cli.project import plan_sha256
from services.cli.review_apply import ApplyError, apply_proposal
from services.cli.review_instructions import canonical_instruction
from services.cli.review_replay import (
    PlanView,
    ProposalOutcome,
    ReviewTranslateError,
    propose_review_command,
)
from services.config.models import ResolvedConfig
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p1_models import ReviewStepRecord
from services.review_command.store import load_head

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

TRANSLATOR_POLICY: Final = Path("config/gates/phase-0c-v1.json")
GAP_PROBE: Final = "全部消して、見せ場だけ残していい感じにしてください"


def _head_view(bundle_file: Path) -> PlanView:
    bundle = load_bundle(bundle_file)
    head = load_head(
        bundle_file.parent / bundle.events_log, bundle_file.parent / bundle.store_dir
    )
    return PlanView(
        plan=head.plan, version=f"v{head.version}", plan_hash=plan_sha256(head.plan)
    )


def _propose(  # noqa: PLR0913, PLR0917 (translator adapter contract)
    manifest: Phase1TechnicalFixtureManifest,
    bundle_file: Path,
    instruction: str,
    policy: ResolvedConfig,
    policy_sha: str,
    translator_sha: str,
) -> ProposalOutcome:
    return propose_review_command(
        manifest,
        instruction,
        _head_view(bundle_file),
        policy,
        policy_sha=policy_sha,
        translator_sha=translator_sha,
    )


def exercise_review_sequence(
    manifest: Phase1TechnicalFixtureManifest,
    run_dir: Path,
    policy_file: Path,
    work: Path,
) -> tuple[list[ReviewStepRecord], OperationOutcome]:
    """Drive the declared correction sequence over the run's live bundle."""

    steps: list[ReviewStepRecord] = []
    bundle_file = run_dir / BUNDLE_NAME
    policy: ResolvedConfig = ResolvedConfig.model_validate_json(policy_file.read_bytes())
    policy_sha = sha256_file(policy_file)
    translator_sha = sha256_file(TRANSLATOR_POLICY)
    work.mkdir(parents=True, exist_ok=True)
    for command in manifest.review_commands:
        try:
            outcome = _propose(
                manifest,
                bundle_file,
                canonical_instruction(command),
                policy,
                policy_sha,
                translator_sha,
            )
        except ReviewTranslateError as error:
            steps.append(
                ReviewStepRecord(
                    command_index=command.command_index,
                    operation=command.operation,
                    status="error",
                    decision="error",
                    structured=False,
                )
            )
            del error
            continue
        proposal_file = work / f"proposal-{command.command_index}.json"
        atomic_write(proposal_file, canonical_model_bytes(outcome))
        structured = outcome.status == "proposal" and outcome.classification is not None
        try:
            result, code = apply_proposal(
                proposal_file, bundle_file, work / f"apply-{command.command_index}"
            )
        except ApplyError:
            steps.append(
                ReviewStepRecord(
                    command_index=command.command_index,
                    operation=command.operation,
                    status=outcome.status,
                    classification=outcome.classification,
                    decision="error",
                    structured=structured,
                    candidate_item_ids=outcome.candidate_item_ids,
                )
            )
            continue
        decision: str = "applied" if result.applied else ("deferred" if code == 0 else "refused")
        steps.append(
            ReviewStepRecord(
                command_index=command.command_index,
                operation=command.operation,
                status=outcome.status,
                classification=result.classification or outcome.classification,
                decision=decision,  # type: ignore[arg-type]
                structured=structured,
                candidate_item_ids=outcome.candidate_item_ids,
                version=result.version,
                plan_sha256=result.plan_sha256,
                ir_sha256=result.ir_sha256,
                preview_sha256=result.preview_sha256,
            )
        )
    gap = _schema_gap_probe(manifest, bundle_file, policy, policy_sha, translator_sha, work)
    return steps, gap


def _schema_gap_probe(  # noqa: PLR0913, PLR0917 (probe adapter contract)
    manifest: Phase1TechnicalFixtureManifest,
    bundle_file: Path,
    policy: ResolvedConfig,
    policy_sha: str,
    translator_sha: str,
    work: Path,
) -> OperationOutcome:
    try:
        outcome = _propose(manifest, bundle_file, GAP_PROBE, policy, policy_sha, translator_sha)
    except ReviewTranslateError as error:
        return OperationOutcome(name="schema-gap-refused", result="refused", detail=error.code)
    atomic_write(work / "proposal-gap.json", canonical_model_bytes(outcome))
    refused = outcome.status == "error" and outcome.error_code == "replay_mismatch"
    return OperationOutcome(
        name="schema-gap-refused",
        result="refused" if refused else (outcome.error_code or outcome.status),
        detail=outcome.error_code or "",
    )


__all__ = ["GAP_PROBE", "TRANSLATOR_POLICY", "exercise_review_sequence"]
