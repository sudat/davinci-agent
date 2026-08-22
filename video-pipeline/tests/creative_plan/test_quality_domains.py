"""QualityDomainReportV1 + seven-domain gate tests (task 40).

PRD 14.3 / implementation plan 8.11: before Final Review the system emits a
report covering ALL SEVEN quality domains with an explicit status each —
applied | intentionally_not_needed | manual_fallback_required | blocked.
A video cannot pass merely because several effects were invoked: the gate
reads statuses only, and NO effect-count quota exists anywhere.

Locked behaviors:

(a) 7-domain completeness — missing / duplicated / unknown / reordered
    domain entries are typed rejections;
(b) all seven explicit (mixed applied / not-needed / manual) -> gate pass;
(c) one blocked domain -> typed reject naming the domain;
(d) manual_fallback_required -> gate passes WITH the surfaced items list
    the Cockpit displays;
(e) justification contract — non-applied without justification rejected,
    applied without evidence rejected, justification on applied rejected;
(f) no-quota structural check — no count-threshold symbols in the module,
    no len() in the gate function body;
(g) round-trip through JSON + double-build determinism;
(h) facts -> status mapping (parametrized): dialogue-free -> subtitle
    intentionally_not_needed with justification; subtitle plan + cues ->
    applied; audio plan present -> applied; color preferred_path external
    + executed -> applied; manual fallback / failed execution outcomes;
    missing evidence and episode-mismatch typed builder refusals.

Adversarial probes: malformed_input = (a)/(e)/builder refusals;
stale_state = (g); dirty_worktree = commit scope check (see evidence).
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)
from services.creative_plan.audio_finishing import (
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    build_audio_plan,
)
from services.creative_plan.color_finishing import (
    ColorFactsV1,
    ColorPlanPolicy,
    build_color_plan,
)
from services.creative_plan.quality_domains import (
    DOMAIN_EXECUTION_OUTCOMES,
    QUALITY_DOMAINS,
    DomainExecutionOutcome,
    DomainExecutionV1,
    ExecutionFactsV1,
    QualityDomainEntryV1,
    QualityDomainError,
    QualityDomainName,
    QualityDomainReportV1,
    QualityDomainStatus,
    QualityFactsV1,
    QualityGateResult,
    QualityPlansV1,
    build_domain_report,
    evaluate_quality_gate,
)
from services.creative_plan.subtitle_models import (
    PATH_ORDER,
    SubtitleCapabilityPathV1,
    SubtitlePlanCueV1,
    SubtitlePlanV1,
    SubtitleStyleProfileV1,
)
from services.final_review.quality_gate import (
    QualityGateRefusal,
    require_quality_gate_pass,
)

EPISODE = "ep-quality-01"
RATE = RationalFrameRate(num=30, den=1)

OUTCOME_STATUS_EXPECTATIONS: dict[DomainExecutionOutcome, QualityDomainStatus] = {
    "executed": "applied",
    "manual_fallback": "manual_fallback_required",
    "failed": "blocked",
}


def _all_applied() -> dict[QualityDomainName, QualityDomainStatus]:
    return dict.fromkeys(QUALITY_DOMAINS, "applied")


# Count-threshold vocabulary forbidden in any CODE identifier of the module
# (structural no-quota guard): the gate reads statuses, never effect tallies.
# Docstrings/comments may DISCUSS the prohibition, so the scan is AST-based —
# identifiers only, never prose.
FORBIDDEN_QUOTA_TOKENS: tuple[str, ...] = (
    "quota",
    "effect_count",
    "min_applied",
    "applied_min",
    "required_applied",
    "applied_required",
    "min_effects",
    "effects_min",
    "minimum_effects",
    "count_threshold",
)


# ------------------------------------------------------------- fixtures


_BASE_FACTS = QualityFactsV1(
    episode_id=EPISODE,
    has_dialogue=True,
    editorial_blocking_defect=False,
    editorial_evidence=("editorial checkpoint clear",),
    framing_motion_intended=True,
    framing_motion_evidence=("framing/motion intent ops recorded",),
    graphics_intended=True,
    graphics_evidence=("graphics presentation ops recorded",),
    delivery_qc_passed=True,
    delivery_qc_evidence=("final qc report passed",),
)


def _facts(**overrides: object) -> QualityFactsV1:
    return _BASE_FACTS.model_copy(update=overrides)


def _subtitle_plan(cue_count: int) -> SubtitlePlanV1:
    cues = tuple(
        SubtitlePlanCueV1(
            cue_id=f"cue-{index + 1}",
            transcript_ref=f"seg-{index + 1}",
            source_id="src-cam-a",
            lines=("こんにちは世界",),
            source_span=SourceFrameSpan(
                start_frame=30 * index, end_frame=30 * (index + 1), rate=RATE
            ),
            record_span=RecordFrameSpan(start_frame=30 * index, end_frame=30 * (index + 1)),
        )
        for index in range(cue_count)
    )
    return SubtitlePlanV1(
        schema_version="subtitle-plan-v1",
        episode_id=EPISODE,
        rate=RATE,
        style_profile=SubtitleStyleProfileV1(profile_id="sub-style"),
        filler_policy="retain",
        cues=cues,
        capability_path=SubtitleCapabilityPathV1(
            ordered_paths=PATH_ORDER,
            selected="native_text_plus",
            matrix_capability="subtitle-capability",
            matrix_status="accepted",
            note="fixture",
        ),
    )


def _audio_plan(episode_id: str = EPISODE):
    return build_audio_plan(
        AudioFactsV1(
            episode_id=episode_id,
            dialogue_clean=True,
            has_bgm=True,
            has_ambience=True,
            measured_loudness_ok=True,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        capability_statuses={"audio-property-operation": "accepted", "voice-isolation": "accepted"},
    )


def _color_plan(color_grade: str = "not_available", advanced_qc: str = "not_available"):
    return build_color_plan(
        ColorFactsV1(episode_id=EPISODE),
        policy=ColorPlanPolicy(
            color_grade_status=color_grade,  # type: ignore[arg-type]
            advanced_qc_status=advanced_qc,  # type: ignore[arg-type]
        ),
    )


def _plans(*, subtitle: int | None = 2, audio: bool = True, color: bool = True) -> QualityPlansV1:
    return QualityPlansV1(
        subtitle_plan=_subtitle_plan(subtitle) if subtitle is not None else None,
        audio_plan=_audio_plan() if audio else None,
        color_plan=_color_plan() if color else None,
    )


def _execution(*entries: DomainExecutionV1) -> ExecutionFactsV1:
    return ExecutionFactsV1(episode_id=EPISODE, domains=entries)


def _report(
    facts: QualityFactsV1 | None = None,
    *,
    plans: QualityPlansV1 | None = None,
    execution: ExecutionFactsV1 | None = None,
) -> QualityDomainReportV1:
    return build_domain_report(
        facts if facts is not None else _facts(),
        plans=plans if plans is not None else _plans(),
        execution_report=execution if execution is not None else _execution(),
    )


def _entries(
    status_by_domain: Mapping[QualityDomainName, QualityDomainStatus],
) -> tuple[QualityDomainEntryV1, ...]:
    """Hand-built entries: applied domains cite evidence, others justify."""
    return tuple(
        QualityDomainEntryV1(
            domain=domain,
            status=status_by_domain[domain],
            evidence_refs=(f"evidence:{domain}",) if status_by_domain[domain] == "applied" else (),
            justification=None if status_by_domain[domain] == "applied" else f"why:{domain}",
        )
        for domain in QUALITY_DOMAINS
    )


# ------------------------------------------- (a) 7-domain completeness


def test_report_missing_domain_rejected() -> None:
    entries = _entries(_all_applied())
    with pytest.raises(ValidationError, match="domain_set_mismatch"):
        QualityDomainReportV1(
            schema_version="quality-domain-report-v1",
            episode_id=EPISODE,
            domains=entries[:6],
        )


def test_report_duplicate_or_unknown_or_reordered_domain_rejected() -> None:
    base = _all_applied()
    with pytest.raises(ValidationError, match="domain_set_mismatch"):
        QualityDomainReportV1(
            schema_version="quality-domain-report-v1",
            episode_id=EPISODE,
            domains=(*_entries(base)[:6], _entries(base)[0]),  # duplicate
        )
    with pytest.raises(ValidationError, match="literal_error"):
        QualityDomainReportV1(
            schema_version="quality-domain-report-v1",
            episode_id=EPISODE,
            domains=(
                *_entries(base)[:6],
                QualityDomainEntryV1.model_validate(
                    {
                        "domain": "b_roll_relevance",  # unknown: outside the vocabulary
                        "status": "applied",
                        "evidence_refs": ("evidence",),
                    }
                ),
            ),
        )
    reordered = _entries(base)
    with pytest.raises(ValidationError, match="domain_set_mismatch"):
        QualityDomainReportV1(
            schema_version="quality-domain-report-v1",
            episode_id=EPISODE,
            domains=(*reordered[1:], reordered[0]),  # wrong canonical order
        )


def test_seven_domains_constant_is_the_prd_set() -> None:
    assert QUALITY_DOMAINS == (
        "editorial_construction",
        "subtitle",
        "audio_finishing",
        "color_finishing",
        "framing_motion",
        "graphics_presentation",
        "delivery_qc",
    )


# ------------------------------------- (b)/(c)/(d) gate decisions


def test_gate_passes_when_all_domains_explicit() -> None:
    statuses = _all_applied()
    statuses["framing_motion"] = "intentionally_not_needed"
    statuses["color_finishing"] = "manual_fallback_required"
    report = QualityDomainReportV1(
        schema_version="quality-domain-report-v1",
        episode_id=EPISODE,
        domains=_entries(statuses),
    )
    result = evaluate_quality_gate(report)
    assert result.decision == "pass"
    assert result.blocked_domains == ()
    assert result.surfaced_manual_items == ("color_finishing",)


def test_gate_rejects_blocked_domain_naming_it() -> None:
    statuses = _all_applied()
    statuses["delivery_qc"] = "blocked"
    report = QualityDomainReportV1(
        schema_version="quality-domain-report-v1",
        episode_id=EPISODE,
        domains=_entries(statuses),
    )
    result = evaluate_quality_gate(report)
    assert result.decision == "reject"
    assert result.blocked_domains == ("delivery_qc",)


def test_gate_rejects_with_every_blocked_domain_named() -> None:
    statuses = _all_applied()
    statuses["subtitle"] = "blocked"
    statuses["audio_finishing"] = "blocked"
    report = QualityDomainReportV1(
        schema_version="quality-domain-report-v1",
        episode_id=EPISODE,
        domains=_entries(statuses),
    )
    result = evaluate_quality_gate(report)
    assert result.decision == "reject"
    assert result.blocked_domains == ("subtitle", "audio_finishing")


def test_gate_result_parity_is_recomputed() -> None:
    with pytest.raises(ValidationError, match="gate_decision_mismatch"):
        QualityGateResult(decision="pass", blocked_domains=("subtitle",))


# --------------------------------------------- (e) justification contract


@pytest.mark.parametrize(
    "status", ["intentionally_not_needed", "manual_fallback_required", "blocked"]
)
def test_non_applied_without_justification_rejected(status: QualityDomainStatus) -> None:
    with pytest.raises(ValidationError, match="justification_required"):
        QualityDomainEntryV1(domain="subtitle", status=status, evidence_refs=())


def test_blank_justification_rejected() -> None:
    with pytest.raises(ValidationError, match="justification_required"):
        QualityDomainEntryV1(domain="subtitle", status="blocked", justification="   ")


def test_applied_requires_evidence_and_forbids_justification() -> None:
    with pytest.raises(ValidationError, match="applied_requires_evidence"):
        QualityDomainEntryV1(domain="subtitle", status="applied", evidence_refs=())
    with pytest.raises(ValidationError, match="justification_only_for_non_applied"):
        QualityDomainEntryV1(
            domain="subtitle",
            status="applied",
            evidence_refs=("evidence",),
            justification="should not be here",
        )


# ------------------------------------------- (f) no-quota static check


def test_no_quota_symbols_in_quality_domain_module() -> None:
    source_file = inspect.getsourcefile(build_domain_report)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Name(id=name) | ast.arg(arg=name) | ast.Attribute(attr=name):
                identifiers.add(name)
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                identifiers.add(name)
            case ast.keyword(arg=name) if name is not None:
                identifiers.add(name)
    for token in FORBIDDEN_QUOTA_TOKENS:
        for identifier in identifiers:
            assert token not in identifier, (
                f"count-threshold identifier {identifier!r} (matches {token!r}) "
                "is forbidden: the gate reads statuses only"
            )


def test_gate_function_reads_statuses_only() -> None:
    gate_source = inspect.getsource(evaluate_quality_gate)
    assert "len(" not in gate_source
    assert "sum(" not in gate_source


# --------------------------------------------------- (g) round-trip


def test_round_trip_and_double_build_determinism() -> None:
    report = _report(
        _facts(),
        plans=_plans(),
        execution=_execution(
            DomainExecutionV1(domain="color_finishing", outcome="manual_fallback")
        ),
    )
    clone = QualityDomainReportV1.model_validate_json(report.model_dump_json())
    assert clone == report
    again = build_domain_report(
        _facts(),
        plans=_plans(),
        execution_report=_execution(
            DomainExecutionV1(domain="color_finishing", outcome="manual_fallback")
        ),
    )
    assert again.model_dump_json() == report.model_dump_json()


# ------------------------------------ (h) facts -> status mapping


def test_subtitle_dialogue_free_is_intentionally_not_needed_with_justification() -> None:
    report = _report(_facts(has_dialogue=False), plans=_plans(subtitle=None))
    entry = report.domain_entry("subtitle")
    assert entry.status == "intentionally_not_needed"
    assert entry.justification is not None
    assert "dialogue-free" in entry.justification
    assert evaluate_quality_gate(report).decision == "pass"


def test_subtitle_plan_with_cues_is_applied() -> None:
    report = _report()
    entry = report.domain_entry("subtitle")
    assert entry.status == "applied"
    assert entry.evidence_refs
    assert "subtitle-plan-v1" in entry.evidence_refs[0]


def test_subtitle_missing_plan_with_dialogue_is_blocked() -> None:
    report = _report(_facts(), plans=_plans(subtitle=None))
    assert report.domain_entry("subtitle").status == "blocked"
    result = evaluate_quality_gate(report)
    assert result.decision == "reject"
    assert "subtitle" in result.blocked_domains


def test_audio_plan_present_is_applied_without_execution_row() -> None:
    report = _report()
    entry = report.domain_entry("audio_finishing")
    assert entry.status == "applied"
    assert entry.evidence_refs
    assert "audio-finishing-plan-v1" in entry.evidence_refs[0]


@pytest.mark.parametrize(
    ("color_grade", "advanced_qc", "expected_path"),
    [
        ("accepted", "accepted", "mcp_live_grading"),
        ("failed", "accepted", "advanced_drx_qc"),
        ("not_available", "not_available", "external"),
    ],
)
def test_color_executed_on_any_path_is_applied(
    color_grade: str, advanced_qc: str, expected_path: str
) -> None:
    plans = QualityPlansV1(
        subtitle_plan=_subtitle_plan(2),
        audio_plan=_audio_plan(),
        color_plan=_color_plan(color_grade, advanced_qc),
    )
    assert plans.color_plan is not None
    assert plans.color_plan.preferred_path == expected_path
    report = _report(
        _facts(),
        plans=plans,
        execution=_execution(DomainExecutionV1(domain="color_finishing", outcome="executed")),
    )
    entry = report.domain_entry("color_finishing")
    assert entry.status == "applied"
    assert entry.evidence_refs
    assert expected_path in entry.evidence_refs[0]


@pytest.mark.parametrize("outcome", DOMAIN_EXECUTION_OUTCOMES)
def test_execution_outcomes_map_to_domain_statuses(outcome: DomainExecutionOutcome) -> None:
    report = _report(
        _facts(),
        execution=_execution(
            DomainExecutionV1(domain="audio_finishing", outcome=outcome),
            DomainExecutionV1(domain="color_finishing", outcome=outcome),
        ),
    )
    expected = OUTCOME_STATUS_EXPECTATIONS[outcome]
    assert report.domain_entry("audio_finishing").status == expected
    assert report.domain_entry("color_finishing").status == expected
    if outcome == "manual_fallback":
        result = evaluate_quality_gate(report)
        assert result.decision == "pass"
        assert "audio_finishing" in result.surfaced_manual_items


def test_editorial_defect_blocks_editorial_construction() -> None:
    report = _report(_facts(editorial_blocking_defect=True))
    entry = report.domain_entry("editorial_construction")
    assert entry.status == "blocked"
    assert entry.justification


def test_delivery_qc_failure_blocks_delivery_domain() -> None:
    report = _report(_facts(delivery_qc_passed=False))
    entry = report.domain_entry("delivery_qc")
    assert entry.status == "blocked"
    assert evaluate_quality_gate(report).decision == "reject"


def test_framing_graphics_not_intended_are_intentionally_not_needed() -> None:
    report = _report(
        _facts(
            framing_motion_intended=False,
            framing_motion_evidence=(),
            graphics_intended=False,
            graphics_evidence=(),
        )
    )
    assert report.domain_entry("framing_motion").status == "intentionally_not_needed"
    assert report.domain_entry("graphics_presentation").status == "intentionally_not_needed"
    assert evaluate_quality_gate(report).decision == "pass"


def test_missing_evidence_for_intended_domain_is_typed_refusal() -> None:
    with pytest.raises(QualityDomainError, match="missing-evidence"):
        _report(_facts(framing_motion_evidence=()))


def test_episode_mismatch_is_typed_refusal() -> None:
    plans = QualityPlansV1(
        subtitle_plan=_subtitle_plan(2),
        audio_plan=_audio_plan(episode_id="ep-other"),
        color_plan=_color_plan(),
    )
    with pytest.raises(QualityDomainError, match="episode-mismatch"):
        _report(_facts(), plans=plans)
    with pytest.raises(QualityDomainError, match="episode-mismatch"):
        _report(
            _facts(),
            plans=_plans(),
            execution=ExecutionFactsV1(
                episode_id="ep-other",
                domains=(DomainExecutionV1(domain="subtitle", outcome="executed"),),
            ),
        )


# -------------------------------- final_review additive wiring


def test_final_review_refuses_while_blocked() -> None:
    report = _report(_facts(delivery_qc_passed=False))
    with pytest.raises(QualityGateRefusal, match="quality-domains-blocked"):
        require_quality_gate_pass(report)


def test_final_review_pass_surfaces_manual_items() -> None:
    report = _report(
        _facts(),
        execution=_execution(
            DomainExecutionV1(domain="color_finishing", outcome="manual_fallback")
        ),
    )
    result = require_quality_gate_pass(report)
    assert result.decision == "pass"
    assert result.surfaced_manual_items == ("color_finishing",)
