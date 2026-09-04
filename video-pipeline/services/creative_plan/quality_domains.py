"""QualityDomainReportV1 + the seven-domain quality gate (task 40).

PRD 14.3 / implementation plan 8.11: before Final Review the system
aggregates actual execution/QC into a report covering ALL SEVEN quality
domains, each with an explicit status — applied | intentionally_not_needed
| manual_fallback_required | blocked. A video cannot pass merely because
several effects were invoked: the gate reads STATUSES ONLY and no
effect-count quota exists anywhere in this module (a structural test
freezes that: no count-threshold symbols, no len() in the gate body).

Entry contract (enforced AT THE MODEL, hand-built reports too):

- ``applied`` cites evidence (``evidence_refs`` non-empty) and carries NO
  justification (justification is only meaningful for non-applied);
- every non-applied status carries a non-blank ``justification`` — an
  unjustified skip is unrepresentable (typed ``justification_required``,
  whitespace rejected).

Builder: :func:`build_domain_report` derives per-domain statuses from
observed facts + committed plans + execution outcomes (below); the gate:
:func:`evaluate_quality_gate` rejects while any domain is ``blocked`` and
passes WITH a surfaced manual-items list (the Episode Cockpit displays it
before final approval). The Final Review binding lives additively in
``services.final_review.quality_gate``.

Execution adapter seam (T18/T23 documented-adapter precedent): task 39's
``mcp-execution-report-v1`` is not committed yet, so the execution input
is typed LOCALLY as :class:`ExecutionFactsV1` — one outcome per domain.
When task 39 lands, its report maps here (failed steps -> ``failed``,
manual_finalization rung steps -> ``manual_fallback``, otherwise
``executed``); the domain report itself never changes shape.
"""

# allow: SIZE_OK — task 40 pins this deliverable to the single-module commit
# scope (quality_domains.py): report models + facts/plans/execution inputs +
# builder + gate in one artifact, T34/T35 single-module precedent. 340 pure
# code lines; the builder's per-domain derivation is irreducible fact mapping
# (one explicit branch per PRD 14.3 domain — silent skips are unrepresentable).

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel, to_tuple
from services.creative_plan.audio_finishing import (  # noqa: TC001 (pydantic resolves it at runtime)
    AudioFinishingPlanV1,
)
from services.creative_plan.color_finishing import (  # noqa: TC001 (pydantic resolves it at runtime)
    ColorFinishingPlanV1,
)
from services.creative_plan.subtitle_models import (  # noqa: TC001 (pydantic resolves it at runtime)
    SubtitlePlanV1,
)

QualityDomainName = Literal[
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
]

#: PRD 14.3's seven domains — the report carries exactly these, in order.
QUALITY_DOMAINS: tuple[QualityDomainName, ...] = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)

QualityDomainStatus = Literal[
    "applied",
    "intentionally_not_needed",
    "manual_fallback_required",
    "blocked",
]

#: Outcomes the task-39 adapter derives per domain (see module docstring).
DomainExecutionOutcome = Literal["executed", "manual_fallback", "failed"]
DOMAIN_EXECUTION_OUTCOMES: tuple[DomainExecutionOutcome, ...] = (
    "executed",
    "manual_fallback",
    "failed",
)

#: Status an execution outcome maps onto when a plan exists.
_OUTCOME_STATUS: dict[DomainExecutionOutcome, QualityDomainStatus] = {
    "executed": "applied",
    "manual_fallback": "manual_fallback_required",
    "failed": "blocked",
}


_StrTuple = Annotated[tuple[str, ...], BeforeValidator(to_tuple)]


class DomainNotNeededDecisionV1(StrictModel):
    """One operator decision that a domain is not needed for this episode.

    The §12.2 episode evidence for ``intentionally_not_needed``: a
    runtime record (chapter-title-proposal precedent) carrying the
    operator's own justification — never a bare absence of a request.
    """

    domain: QualityDomainName
    decision: Literal["not_needed"]
    justification: str = Field(min_length=1, strict=True)
    decided_by: str = Field(default="operator", min_length=1, strict=True)


class QualityDomainError(ValueError):
    """Typed refusal from the domain-report builder (never silent)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ------------------------------------------------------------- report


class QualityDomainEntryV1(StrictModel):
    """One domain's explicit status — evidence when applied, else justified.

    ``proposed`` marks a domain whose episode evidence says treatment is
    needed but no committed plan exists yet (§12.2: blocked until the
    operator acts — a proposal, never an auto-apply); ``proposal_basis``
    carries the citing evidence lines.
    """

    domain: QualityDomainName
    status: QualityDomainStatus
    evidence_refs: _StrTuple = ()
    justification: str | None = None
    proposed: bool = Field(default=False, strict=True)
    proposal_basis: _StrTuple = ()

    @model_validator(mode="after")
    def require_status_contract(self) -> QualityDomainEntryV1:
        match self.status:
            case "applied":
                if not self.evidence_refs:
                    raise PydanticCustomError(
                        "applied_requires_evidence",
                        "an applied domain must cite evidence",
                    )
                if self.justification is not None:
                    raise PydanticCustomError(
                        "justification_only_for_non_applied",
                        "justification is only meaningful on a non-applied domain",
                    )
            case "intentionally_not_needed" | "manual_fallback_required" | "blocked":
                if not (self.justification and self.justification.strip()):
                    raise PydanticCustomError(
                        "justification_required",
                        "domain {domain} status {status} requires a justification",
                        {"domain": self.domain, "status": self.status},
                    )
        return self


class QualityDomainReportV1(StrictModel):
    """The seven-domain finishing report (PRD 14.3; schema v1)."""

    schema_version: Literal["quality-domain-report-v1"]
    episode_id: Identifier
    domains: Annotated[tuple[QualityDomainEntryV1, ...], BeforeValidator(to_tuple)]

    @model_validator(mode="after")
    def require_all_seven_domains(self) -> QualityDomainReportV1:
        names = tuple(entry.domain for entry in self.domains)
        if names != QUALITY_DOMAINS:
            raise PydanticCustomError(
                "domain_set_mismatch",
                "domains must be exactly the seven quality domains in canonical order: {domains}",
                {"domains": list(QUALITY_DOMAINS)},
            )
        return self

    def domain_entry(self, domain: QualityDomainName) -> QualityDomainEntryV1:
        """The one entry for ``domain`` (completeness makes it total)."""
        for entry in self.domains:
            if entry.domain == domain:
                return entry
        raise QualityDomainError(  # pragma: no cover - unrepresentable
            "unknown-domain", f"domain {domain} is not one of the seven"
        )


# ------------------------------------------------- builder inputs


class QualityFactsV1(StrictModel):
    """Observed episode facts the report derives from (never plan-overridable).

    Evidence tuples back the domains the facts alone turn ``applied``; the
    builder refuses (typed) to mark a domain applied without evidence.
    """

    episode_id: Identifier
    has_dialogue: bool
    editorial_blocking_defect: bool
    editorial_evidence: _StrTuple = ()
    framing_motion_intended: bool
    framing_motion_evidence: _StrTuple = ()
    framing_motion_committed_evidence: _StrTuple = ()
    graphics_intended: bool
    graphics_evidence: _StrTuple = ()
    graphics_committed_evidence: _StrTuple = ()
    operator_not_needed: Annotated[
        tuple[DomainNotNeededDecisionV1, ...], BeforeValidator(to_tuple)
    ] = ()
    delivery_qc_passed: bool
    delivery_qc_evidence: _StrTuple = ()


class DomainExecutionV1(StrictModel):
    """One domain's execution outcome (adapter seam — module docstring)."""

    domain: QualityDomainName
    outcome: DomainExecutionOutcome


class ExecutionFactsV1(StrictModel):
    """Adapter seam for task 39's ``mcp-execution-report-v1`` (module docstring)."""

    episode_id: Identifier
    domains: Annotated[tuple[DomainExecutionV1, ...], BeforeValidator(to_tuple)] = ()

    @model_validator(mode="after")
    def require_unique_domains(self) -> ExecutionFactsV1:
        names = [entry.domain for entry in self.domains]
        if len(set(names)) != len(names):
            raise PydanticCustomError("duplicate_execution_domain", "execution domains are unique")
        return self

    def outcome_for(self, domain: QualityDomainName) -> DomainExecutionOutcome | None:
        for entry in self.domains:
            if entry.domain == domain:
                return entry.outcome
        return None


class QualityPlansV1(StrictModel):
    """The committed finishing plans the report aggregates over."""

    subtitle_plan: SubtitlePlanV1 | None = None
    audio_plan: AudioFinishingPlanV1 | None = None
    color_plan: ColorFinishingPlanV1 | None = None


# -------------------------------------------------------- builder


def _applied(domain: QualityDomainName, evidence: tuple[str, ...]) -> QualityDomainEntryV1:
    return QualityDomainEntryV1(domain=domain, status="applied", evidence_refs=evidence)


def _with_status(
    domain: QualityDomainName,
    status: QualityDomainStatus,
    justification: str,
) -> QualityDomainEntryV1:
    return QualityDomainEntryV1(domain=domain, status=status, justification=justification)


def _require_applied_evidence(domain: str, evidence: tuple[str, ...]) -> None:
    if not evidence:
        raise QualityDomainError(
            "missing-evidence",
            f"{domain} is applied by the episode facts but carries no evidence",
        )


def _editorial_entry(facts: QualityFactsV1) -> QualityDomainEntryV1:
    if facts.editorial_blocking_defect:
        return _with_status(
            "editorial_construction",
            "blocked",
            "a blocking continuity/story defect is recorded in the episode facts",
        )
    _require_applied_evidence("editorial_construction", facts.editorial_evidence)
    return _applied("editorial_construction", facts.editorial_evidence)


def _subtitle_entry(facts: QualityFactsV1, plans: QualityPlansV1) -> QualityDomainEntryV1:
    if not facts.has_dialogue:
        return _with_status(
            "subtitle",
            "intentionally_not_needed",
            "dialogue-free episode: no subtitle material exists",
        )
    subtitle_plan = plans.subtitle_plan
    if subtitle_plan is None:
        return _with_status(
            "subtitle",
            "blocked",
            "dialogue present but no subtitle plan exists (silent skip forbidden)",
        )
    cue_count = len(subtitle_plan.cues)
    if cue_count == 0:
        return _with_status(
            "subtitle",
            "blocked",
            "dialogue present but the subtitle plan carries no cues",
        )
    return _applied("subtitle", (f"subtitle-plan-v1: {cue_count} cues",))


def _plan_driven_entry(
    domain: QualityDomainName,
    plan: SubtitlePlanV1 | AudioFinishingPlanV1 | ColorFinishingPlanV1 | None,
    plan_evidence: tuple[str, ...],
    execution: ExecutionFactsV1,
    no_plan_justification: str,
) -> QualityDomainEntryV1:
    if plan is None:
        return _with_status(domain, "blocked", no_plan_justification)
    match execution.outcome_for(domain):
        case "manual_fallback":
            return _with_status(
                domain,
                "manual_fallback_required",
                "execution fell back to the manual finalization rung; the item "
                "is surfaced for the Cockpit before final approval",
            )
        case "failed":
            return _with_status(
                domain,
                "blocked",
                "execution failed and was not recovered on the fallback ladder",
            )
        case "executed" | None:
            return _applied(domain, plan_evidence)


def _evidence_driven_entry(
    domain: QualityDomainName,
    *,
    intended: bool,
    evidence: tuple[str, ...],
    committed: tuple[str, ...],
    override: DomainNotNeededDecisionV1 | None,
) -> QualityDomainEntryV1:
    """The §12.2 semantics: not-needed only on episode evidence, a needed
    domain without a committed plan is a surfaced PROPOSAL (blocked), and
    an operator decision overrides evidence of need."""
    label = domain.replace("_", " ")
    if override is not None:
        return QualityDomainEntryV1(
            domain=domain,
            status="intentionally_not_needed",
            justification=f"operator decision ({override.decided_by}): {override.justification}",
        )
    if intended:
        if not evidence:
            raise QualityDomainError(
                "missing-evidence",
                f"{domain} is intended by the episode facts but carries no evidence",
            )
        if committed:
            return _applied(domain, committed)
        return QualityDomainEntryV1(
            domain=domain,
            status="blocked",
            justification=(
                f"episode evidence proposes {label} treatment but no committed plan "
                "exists (operator action required; proposal only, never auto-applied)"
            ),
            proposed=True,
            proposal_basis=evidence,
        )
    justification = (
        "; ".join(evidence)
        if evidence
        else f"no episode evidence of {label} need was recorded"
    )
    return _with_status(domain, "intentionally_not_needed", justification)


def _operator_override(
    facts: QualityFactsV1, domain: QualityDomainName
) -> DomainNotNeededDecisionV1 | None:
    return next(
        (row for row in facts.operator_not_needed if row.domain == domain), None)


def build_domain_report(
    facts: QualityFactsV1,
    *,
    plans: QualityPlansV1,
    execution_report: ExecutionFactsV1,
) -> QualityDomainReportV1:
    """Derive the seven-domain report from facts + plans + execution outcomes.

    Raises :class:`QualityDomainError` (typed, never silent) on episode-id
    mismatch across inputs, and on facts that mark a domain applied without
    any evidence to cite.
    """
    for label, plan in (
        ("subtitle_plan", plans.subtitle_plan),
        ("audio_plan", plans.audio_plan),
        ("color_plan", plans.color_plan),
    ):
        if plan is not None and plan.episode_id != facts.episode_id:
            raise QualityDomainError(
                "episode-mismatch",
                f"{label} episode {plan.episode_id} != facts episode {facts.episode_id}",
            )
    if execution_report.episode_id != facts.episode_id:
        raise QualityDomainError(
            "episode-mismatch",
            f"execution_report episode {execution_report.episode_id} != facts "
            f"episode {facts.episode_id}",
        )

    audio_plan = plans.audio_plan
    audio_evidence = (
        (f"audio-finishing-plan-v1: {len(audio_plan.stages)} ladder stages",)
        if audio_plan is not None
        else ()
    )
    color_plan = plans.color_plan
    color_evidence = (
        (f"color-finishing-plan-v1: preferred_path={color_plan.preferred_path}",)
        if color_plan is not None
        else ()
    )

    framing_evidence = facts.framing_motion_evidence
    if facts.framing_motion_intended:
        _require_applied_evidence("framing_motion", framing_evidence)
    graphics_evidence = facts.graphics_evidence
    if facts.graphics_intended:
        _require_applied_evidence("graphics_presentation", graphics_evidence)
    if facts.delivery_qc_passed:
        _require_applied_evidence("delivery_qc", facts.delivery_qc_evidence)

    domains = (
        _editorial_entry(facts),
        _subtitle_entry(facts, plans),
        _plan_driven_entry(
            "audio_finishing",
            audio_plan,
            audio_evidence,
            execution_report,
            "no explicit audio finishing plan exists (plan/result is required)",
        ),
        _plan_driven_entry(
            "color_finishing",
            color_plan,
            color_evidence,
            execution_report,
            "no explicit color finishing plan exists (correction/match/look "
            "result or a justified no-op is required)",
        ),
        _evidence_driven_entry(
            "framing_motion",
            intended=facts.framing_motion_intended,
            evidence=framing_evidence,
            committed=facts.framing_motion_committed_evidence,
            override=_operator_override(facts, "framing_motion"),
        ),
        _evidence_driven_entry(
            "graphics_presentation",
            intended=facts.graphics_intended,
            evidence=graphics_evidence,
            committed=facts.graphics_committed_evidence,
            override=_operator_override(facts, "graphics_presentation"),
        ),
        _applied("delivery_qc", facts.delivery_qc_evidence)
        if facts.delivery_qc_passed
        else _with_status(
            "delivery_qc",
            "blocked",
            "delivery QC did not pass for this episode",
        ),
    )
    return QualityDomainReportV1(
        schema_version="quality-domain-report-v1",
        episode_id=facts.episode_id,
        domains=domains,
    )


# ------------------------------------------------------------ gate


class QualityGateResult(StrictModel):
    """Statuses-only gate decision: blocked rejects, manual items surface."""

    decision: Literal["pass", "reject"]
    blocked_domains: tuple[QualityDomainName, ...] = ()
    surfaced_manual_items: tuple[QualityDomainName, ...] = ()

    @model_validator(mode="after")
    def require_recomputed_decision(self) -> QualityGateResult:
        if self.decision == "reject" and not self.blocked_domains:
            raise PydanticCustomError(
                "gate_decision_mismatch",
                "a reject must name at least one blocked domain",
            )
        if self.decision == "pass" and self.blocked_domains:
            raise PydanticCustomError(
                "gate_decision_mismatch",
                "a pass cannot carry blocked domains",
            )
        return self


def evaluate_quality_gate(report: QualityDomainReportV1) -> QualityGateResult:
    """Reject while any domain is blocked; surface manual-fallback items.

    This gate reads statuses only — it never inspects, weighs, or tallies
    how many effects were applied (PRD 14.3: no effect-count quota).
    """
    blocked: tuple[QualityDomainName, ...] = tuple(
        entry.domain for entry in report.domains if entry.status == "blocked"
    )
    surfaced: tuple[QualityDomainName, ...] = tuple(
        entry.domain for entry in report.domains if entry.status == "manual_fallback_required"
    )
    return QualityGateResult(
        decision="reject" if blocked else "pass",
        blocked_domains=blocked,
        surfaced_manual_items=surfaced,
    )


__all__ = [
    "DOMAIN_EXECUTION_OUTCOMES",
    "QUALITY_DOMAINS",
    "DomainExecutionOutcome",
    "DomainExecutionV1",
    "DomainNotNeededDecisionV1",
    "ExecutionFactsV1",
    "QualityDomainEntryV1",
    "QualityDomainError",
    "QualityDomainName",
    "QualityDomainReportV1",
    "QualityDomainStatus",
    "QualityFactsV1",
    "QualityGateResult",
    "QualityPlansV1",
    "build_domain_report",
    "evaluate_quality_gate",
]
