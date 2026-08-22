"""Task 52: three-episode measurement harness (episode-run-report-v1).

Locks EpisodeRunReportV1's measured field set to implementation-plan §10.4
(docs/prd/implementation-plan-v4.3.md:1074-1101) verbatim, proves
``collect_episode_run`` fills fields from synthetic episode roots with
EXPLICIT nulls + named coverage gaps for anything absent, and exercises the
Phase-6 aggregation (non-talking-head failure zero, automation-rate
estimate) and the pre-start footage-source decision. No 30-minute-target
claim is asserted anywhere — this harness exists for product-scope
validation only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli.episode_report import main as episode_report_main
from services.metrics.episode_run_report import (
    EPISODE_CLASSES,
    MEASURE_FIELDS,
    AutomationRateEstimateV1,
    EpisodeRunReportError,
    EpisodeRunReportV1,
    FootageDecisionV1,
    FootageOperatorInputs,
    OperatorEpisodeMeasurements,
    Phase6SummaryV1,
    aggregate_three_episodes,
    apply_measurements,
    check_footage_sources,
    collect_episode_run,
)

# The implementation-plan §10.4 measurement list, hardcoded independently of
# the schema so drift in either direction fails the lock below.
IMPL_PLAN_10_4_KEYS = (
    "total_source_minutes",
    "output_minutes",
    "ttfrp_minutes",
    "total_wall_clock_minutes",
    "universal_pass_wall_clock_minutes",
    "deep_review_wall_clock_minutes",
    "deep_review_source_minute_ratio",
    "vision_frames",
    "vision_tokens",
    "vision_cost_usd",
    "cache_hit_ratio",
    "human_review_minutes",
    "human_blocking_sessions",
    "manual_resolve_edit_minutes",
    "keep_remove_corrections",
    "broll_corrections",
    "subtitle_corrections",
    "effect_corrections",
    "missed_valuable_moments",
    "blocking_defects",
    "manual_fallback_capabilities",
    "domain_statuses",
    "interruption_counts",
    "publishability_publishable",
    "publishability_reasons",
    "taste_evidence_used",
    "kit_recipes_used",
    "kit_recipes_overridden",
    "kit_recipes_manual_corrected",
    "cli_required",
    "json_required",
    "direct_resolve_required",
)

REPORT_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "episode_class",
        "episode_id",
        "class_attributed_failure",
        "evidence_refs",
        "field_coverage",
    }
)

SEVEN_DOMAINS = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)


# ------------------------------------------------------------------
# synthetic episode-root builders (tmp_path only; no real footage)
# ------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    return path


def _episode0_run_payload(*, duration_seconds: float | None, probed: bool) -> dict:
    return {
        "schema_version": "episode0-report-v1",
        "run_id": "2026-08-23",
        "manifest": {
            "schema_version": "episode0-source-manifest-v1",
            "video_path": "source.mp4",
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "duration_seconds": duration_seconds,
            "duration_probed": probed,
        },
        "log": {
            "schema_version": "episode0-baseline-v1",
            "active_human_time_minutes": 25.5,
            "ttfrp_minutes": 12.5,
            "wall_clock_minutes": 180.0,
            "manual_resolve_minutes": 3.0,
            "wrong_keep_remove": [
                {"ts": "00:05:00", "note": "weak joke kept"},
                {"ts": "00:09:00", "note": "strong take removed"},
            ],
            "missed_moments": [{"ts": "00:22:00", "note": "best demo skipped"}],
            "finishing_deficits": [],
            "interruption_points": [{"ts": "00:40:00", "note": "subtitle font ask"}],
            "publishability": {
                "publishable": True,
                "comment": "solid cut, pacing holds",
                "best_ts": "00:01:30",
                "worst_ts": "00:12:00",
            },
        },
    }


def _quality_domain_payload(
    *, manual_domains: tuple[str, ...] = (), blocked_domains: tuple[str, ...] = ()
) -> dict:
    domains = []
    for name in SEVEN_DOMAINS:
        if name in manual_domains:
            domains.append(
                {
                    "domain": name,
                    "status": "manual_fallback_required",
                    "evidence_refs": [],
                    "justification": "fixture: manual path exercised",
                }
            )
        elif name in blocked_domains:
            domains.append(
                {
                    "domain": name,
                    "status": "blocked",
                    "evidence_refs": [],
                    "justification": "fixture: blocked path exercised",
                }
            )
        else:
            domains.append(
                {"domain": name, "status": "applied", "evidence_refs": [f"evidence:{name}"]}
            )
    return {
        "schema_version": "quality-domain-report-v1",
        "episode_id": "ep-fixture",
        "domains": domains,
    }


def _interruption_payload() -> dict:
    return {
        "counts": {
            "SAFE_AUTO_RESOLVE": 2,
            "DEGRADED_BUT_RECOVERABLE": 1,
            "HUMAN_DECISION_REQUIRED": 0,
        },
        "blocking_count": 0,
        "notices": ["subtitle fallback used", "transient mcp retried"],
    }


def _editorial_qc_payload() -> dict:
    return {
        "schema_version": "editorial-qc-report-v1",
        "candidates": [],
        "counts_by_severity": {"info": 0, "warning": 0, "critical": 0},
        "counts_by_check": {},
    }


def _write_upload_ledger(root: Path) -> Path:
    lines = [
        {"idempotency_key": "up-1", "status": "completed", "video_id": "vid-9", "timestamp": "t1"},
        {"idempotency_key": "up-2", "status": "failed", "reason": "net", "timestamp": "t2"},
    ]
    path = root / "publish" / "main-channel.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _full_operator_measurements() -> OperatorEpisodeMeasurements:
    return OperatorEpisodeMeasurements(
        episode_id="ep-a-full",
        class_attributed_failure=False,
        output_minutes=8.5,
        universal_pass_wall_clock_minutes=40.0,
        deep_review_wall_clock_minutes=90.0,
        deep_review_source_minute_ratio=4.5,
        vision_frames=120,
        vision_tokens=45000,
        vision_cost_usd=1.25,
        cache_hit_ratio=0.62,
        human_review_minutes=18.0,
        human_blocking_sessions=2,
        broll_corrections=1,
        subtitle_corrections=3,
        effect_corrections=0,
        taste_evidence_used=("ref-01", "ref-02"),
        kit_recipes_used=("r-jump-cut",),
        kit_recipes_overridden=("r-bgm",),
        kit_recipes_manual_corrected=(),
        cli_required=False,
        json_required=False,
        direct_resolve_required=False,
    )


def _full_episode_root(tmp_path: Path) -> Path:
    root = tmp_path / "episode-a"
    _write_json(
        root / "runs" / "2026-08-23" / "report.json",
        _episode0_run_payload(duration_seconds=1200.0, probed=True),
    )
    _write_json(root / "artifacts" / "quality-domains.json", _quality_domain_payload())
    _write_json(root / "artifacts" / "interruption-summary.json", _interruption_payload())
    _write_json(root / "artifacts" / "editorial-qc.json", _editorial_qc_payload())
    _write_upload_ledger(root)
    return root


def _collect_full(tmp_path: Path) -> EpisodeRunReportV1:
    return collect_episode_run(
        _full_episode_root(tmp_path),
        episode_class="a_talking_broll",
        operator_measurements=_full_operator_measurements(),
    )


# ------------------------------------------------------------------
# (f) schema lock — the measured set IS implementation-plan §10.4
# ------------------------------------------------------------------


def test_measure_field_set_is_locked_to_impl_plan_10_4() -> None:
    assert len(IMPL_PLAN_10_4_KEYS) == 32
    assert set(MEASURE_FIELDS) == set(IMPL_PLAN_10_4_KEYS)
    assert len(MEASURE_FIELDS) == 32
    model_fields = set(EpisodeRunReportV1.model_fields)
    assert model_fields - REPORT_METADATA_FIELDS == set(MEASURE_FIELDS)
    assert set(EPISODE_CLASSES) == {"a_talking_broll", "b_visual_first", "c_mixed"}


# ------------------------------------------------------------------
# (a) full inputs → every field filled, coverage 100%
# ------------------------------------------------------------------


def test_collect_full_inputs_fills_every_field(tmp_path: Path) -> None:
    report = _collect_full(tmp_path)
    assert report.schema_version == "episode-run-report-v1"
    assert report.episode_class == "a_talking_broll"
    assert report.episode_id == "ep-a-full"
    for name in MEASURE_FIELDS:
        assert getattr(report, name) is not None, f"{name} must be filled from full inputs"
    assert report.field_coverage.filled == 32
    assert report.field_coverage.total == 32
    assert report.field_coverage.gaps == ()
    # episode0-derived values
    assert report.total_source_minutes == pytest.approx(20.0)
    assert report.ttfrp_minutes == pytest.approx(12.5)
    assert report.total_wall_clock_minutes == pytest.approx(180.0)
    assert report.manual_resolve_edit_minutes == pytest.approx(3.0)
    assert report.keep_remove_corrections == 2
    assert report.missed_valuable_moments == 1
    assert report.publishability_publishable is True
    reasons = report.publishability_reasons
    assert reasons is not None
    assert "solid cut, pacing holds" in reasons
    # quality-domain-derived values
    statuses = report.domain_statuses
    assert statuses is not None
    assert set(statuses) == set(SEVEN_DOMAINS)
    assert all(status == "applied" for status in statuses.values())
    assert report.manual_fallback_capabilities == ()
    assert report.blocking_defects == 0
    # interruption summary
    assert report.interruption_counts == {
        "SAFE_AUTO_RESOLVE": 2,
        "DEGRADED_BUT_RECOVERABLE": 1,
        "HUMAN_DECISION_REQUIRED": 0,
    }
    # operator measurements flowed through
    assert report.output_minutes == pytest.approx(8.5)
    assert report.class_attributed_failure is False
    # every consumed artifact is cited as evidence (root-relative posix paths)
    evidence = set(report.evidence_refs)
    assert evidence == {
        "runs/2026-08-23/report.json",
        "artifacts/quality-domains.json",
        "artifacts/interruption-summary.json",
        "artifacts/editorial-qc.json",
        "publish/main-channel.jsonl",
    }


def test_collect_latest_episode0_run_wins(tmp_path: Path) -> None:
    root = tmp_path / "episode-b-runs"
    _write_json(
        root / "runs" / "baseline" / "report.json",
        _episode0_run_payload(duration_seconds=600.0, probed=True),
    )
    newer = _episode0_run_payload(duration_seconds=1200.0, probed=True)
    newer["run_id"] = "2026-08-24"
    _write_json(root / "runs" / "2026-08-24" / "report.json", newer)
    report = collect_episode_run(root, episode_class="b_visual_first")
    assert report.total_source_minutes == pytest.approx(20.0)
    assert "runs/2026-08-24/report.json" in report.evidence_refs


# ------------------------------------------------------------------
# (b) partial inputs → explicit nulls + named coverage gaps
# ------------------------------------------------------------------


def test_collect_partial_inputs_leaves_nulls_and_lists_gaps(tmp_path: Path) -> None:
    root = tmp_path / "episode-partial"
    _write_json(
        root / "runs" / "2026-08-23" / "report.json",
        _episode0_run_payload(duration_seconds=None, probed=False),
    )
    report = collect_episode_run(root, episode_class="a_talking_broll")
    assert report.ttfrp_minutes == pytest.approx(12.5)
    assert report.total_source_minutes is None
    assert report.domain_statuses is None
    assert report.interruption_counts is None
    assert report.output_minutes is None
    coverage = report.field_coverage
    assert coverage.total == 32
    assert coverage.filled == 7  # ttfrp, wall-clock, manual-resolve, keep/remove, missed,
    # publishable, reasons — duration not probed so source minutes stays null
    gap_by_field = {gap.field: gap.reason for gap in coverage.gaps}
    assert set(gap_by_field) == set(MEASURE_FIELDS) - {
        "ttfrp_minutes",
        "total_wall_clock_minutes",
        "manual_resolve_edit_minutes",
        "keep_remove_corrections",
        "missed_valuable_moments",
        "publishability_publishable",
        "publishability_reasons",
    }
    assert "not probed" in gap_by_field["total_source_minutes"]
    assert "quality-domain-report-v1" in gap_by_field["domain_statuses"]
    assert "interruption" in gap_by_field["interruption_counts"]
    assert "operator" in gap_by_field["output_minutes"].lower()
    for field in gap_by_field:
        assert getattr(report, field) is None, f"gap field {field} must be null"


def test_collect_invalid_artifact_reports_gap_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "episode-invalid"
    _write_json(
        root / "runs" / "2026-08-23" / "report.json",
        _episode0_run_payload(duration_seconds=1200.0, probed=True),
    )
    broken = _quality_domain_payload()
    broken["domains"] = broken["domains"][:3]  # not the seven
    _write_json(root / "artifacts" / "quality-domains.json", broken)
    report = collect_episode_run(root, episode_class="c_mixed")
    assert report.domain_statuses is None
    gap_by_field = {gap.field: gap.reason for gap in report.field_coverage.gaps}
    assert "invalid" in gap_by_field["domain_statuses"]


def test_collect_missing_or_file_root_is_typed_error(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-episode"
    with pytest.raises(EpisodeRunReportError) as missing_exc:
        collect_episode_run(missing, episode_class="a_talking_broll")
    assert missing_exc.value.code == "episode-root-missing"
    a_file = tmp_path / "episode.json"
    a_file.write_text("{}", encoding="utf-8")
    with pytest.raises(EpisodeRunReportError) as file_exc:
        collect_episode_run(a_file, episode_class="a_talking_broll")
    assert file_exc.value.code == "episode-root-missing"


def test_coverage_lie_is_rejected(tmp_path: Path) -> None:
    report = _collect_full(tmp_path)
    payload = report.model_dump(mode="json")
    # lie 1: inner accounting itself is inconsistent
    payload["field_coverage"]["filled"] = 31
    with pytest.raises(ValidationError, match="coverage_accounting"):
        EpisodeRunReportV1.model_validate(payload)
    # lie 2: accounting-consistent coverage that invents a gap on a filled field
    lying = report.model_dump(mode="json")
    gap = {"field": "output_minutes", "reason": "operator measurement not provided"}
    lying["field_coverage"] = {"filled": 31, "total": 32, "gaps": [gap]}
    with pytest.raises(ValidationError, match="coverage_mismatch"):
        EpisodeRunReportV1.model_validate(lying)
    # lie 3: a null field hidden from the gap list
    hiding = collect_episode_run(
        _full_episode_root(tmp_path), episode_class="a_talking_broll"
    ).model_dump(mode="json")
    hiding["output_minutes"] = 7.5
    with pytest.raises(ValidationError, match="coverage_mismatch"):
        EpisodeRunReportV1.model_validate(hiding)


def test_apply_measurements_recomputes_coverage(tmp_path: Path) -> None:
    root = _full_episode_root(tmp_path)
    partial = collect_episode_run(root, episode_class="a_talking_broll")
    assert partial.output_minutes is None
    filled_before = partial.field_coverage.filled
    patched = apply_measurements(
        partial,
        OperatorEpisodeMeasurements(output_minutes=9.0, kit_recipes_used=("r-cross-dissolve",)),
    )
    assert patched.output_minutes == pytest.approx(9.0)
    assert patched.kit_recipes_used == ("r-cross-dissolve",)
    assert patched.field_coverage.filled == filled_before + 2
    gap_fields = {gap.field for gap in patched.field_coverage.gaps}
    assert "output_minutes" not in gap_fields
    assert "kit_recipes_used" not in gap_fields
    # original untouched (frozen report, no mutation)
    assert partial.output_minutes is None
    assert partial.field_coverage.filled == filled_before


# ------------------------------------------------------------------
# (c) aggregation over three synthetic classes
# ------------------------------------------------------------------


def _three_episode_reports(tmp_path: Path) -> list[EpisodeRunReportV1]:
    root_a = _full_episode_root(tmp_path)
    report_a = collect_episode_run(
        root_a, episode_class="a_talking_broll", operator_measurements=_full_operator_measurements()
    )
    root_b = tmp_path / "episode-b"
    _write_json(
        root_b / "runs" / "2026-08-23" / "report.json",
        _episode0_run_payload(duration_seconds=900.0, probed=True),
    )
    report_b = collect_episode_run(
        root_b,
        episode_class="b_visual_first",
        operator_measurements=OperatorEpisodeMeasurements(
            class_attributed_failure=False,
            kit_recipes_used=("r-visual-pace", "r-ambience"),
            kit_recipes_manual_corrected=("r-transitions",),
            manual_resolve_edit_minutes=None,
            cli_required=False,
        ),
    )
    root_c = tmp_path / "episode-c"
    root_c.mkdir()
    report_c = collect_episode_run(
        root_c,
        episode_class="c_mixed",
        operator_measurements=OperatorEpisodeMeasurements(class_attributed_failure=False),
    )
    return [report_a, report_b, report_c]


def test_aggregate_three_episodes_summary(tmp_path: Path) -> None:
    reports = _three_episode_reports(tmp_path)
    summary = aggregate_three_episodes(reports)
    assert isinstance(summary, Phase6SummaryV1)
    assert summary.schema_version == "phase6-summary-v1"
    assert tuple(row.episode_class for row in summary.rows) == EPISODE_CLASSES
    # exit criterion: no episode fails BECAUSE it is class b/c
    assert summary.non_talking_head_failure_zero is True
    assert summary.non_talking_head_failure_asserted is True
    # automation estimate: A used 1/overridden 1, B used 2/corrected 1 → 3/(3+2)=0.6
    estimate = summary.automation_rate_estimate
    assert estimate.automated_actions == 3
    assert estimate.manual_actions == 2
    assert estimate.rate == pytest.approx(0.6)
    assert estimate.target_rate == pytest.approx(0.8)
    assert estimate.meets_target is False
    # aggregate coverage accounting
    assert summary.coverage.reports == 3
    assert summary.coverage.total == 96
    assert summary.coverage.filled == sum(r.field_coverage.filled for r in reports)
    assert summary.coverage.complete_reports == 1  # only episode A is complete


def test_aggregate_class_attributed_failure_flips_assertion(tmp_path: Path) -> None:
    reports = _three_episode_reports(tmp_path)
    b_failed = apply_measurements(
        reports[1], OperatorEpisodeMeasurements(class_attributed_failure=True)
    )
    summary = aggregate_three_episodes([reports[0], b_failed, reports[2]])
    assert summary.non_talking_head_failure_zero is False
    assert summary.non_talking_head_failure_asserted is True
    # a talking-head class failure must NOT flip the non-talking-head check
    a_failed = apply_measurements(
        reports[0], OperatorEpisodeMeasurements(class_attributed_failure=True)
    )
    unaffected = aggregate_three_episodes([a_failed, reports[1], reports[2]])
    assert unaffected.non_talking_head_failure_zero is True


def test_aggregate_requires_at_least_one_report() -> None:
    with pytest.raises(EpisodeRunReportError) as exc:
        aggregate_three_episodes([])
    assert exc.value.code == "no-reports"


def test_automation_rate_estimate_invariants() -> None:
    meets = AutomationRateEstimateV1(
        automated_actions=8, manual_actions=2, rate=0.8, meets_target=True
    )
    assert meets.rate == pytest.approx(0.8)
    assert meets.meets_target is True
    no_data = AutomationRateEstimateV1(automated_actions=0, manual_actions=0)
    assert no_data.rate is None
    assert no_data.meets_target is None
    with pytest.raises(ValidationError):
        AutomationRateEstimateV1(automated_actions=0, manual_actions=0, rate=0.5)
    with pytest.raises(ValidationError):
        AutomationRateEstimateV1(automated_actions=1, manual_actions=1, meets_target=None)
    with pytest.raises(ValidationError):
        AutomationRateEstimateV1(
            automated_actions=1, manual_actions=1, rate=0.9, meets_target=False
        )


# ------------------------------------------------------------------
# (d) pre-start footage-source decision
# ------------------------------------------------------------------


def _real01_root(tmp_path: Path) -> Path:
    root = tmp_path / "real-01"
    root.mkdir()
    (root / "episode.json").write_text("{}", encoding="utf-8")
    return root


def test_footage_decision_fallback_proposed_when_a_c_absent(tmp_path: Path) -> None:
    decision = check_footage_sources(
        FootageOperatorInputs(real01_episode_json=str(_real01_root(tmp_path) / "episode.json"))
    )
    assert isinstance(decision, FootageDecisionV1)
    assert decision.schema_version == "footage-decision-v1"
    assert decision.wait_policy == "no_indefinite_wait"
    checks = {check.episode_class: check for check in decision.checks}
    assert set(checks) == set(EPISODE_CLASSES)
    assert checks["b_visual_first"].status == "confirmed"
    assert checks["b_visual_first"].fallback_proposal is None
    for episode_class in ("a_talking_broll", "c_mixed"):
        check = checks[episode_class]
        assert check.status == "fallback_proposed"
        assert check.fallback_proposal is not None
        assert "synthetic screen-capture" in check.fallback_proposal
        assert check.sources == ()


def test_footage_decision_confirmed_when_sources_present(tmp_path: Path) -> None:
    root = tmp_path
    a_source = root / "a-speech-01.mp4"
    a_source.write_bytes(b"fake")
    c_source = root / "c-screen-01.mp4"
    c_source.write_bytes(b"fake")
    decision = check_footage_sources(
        FootageOperatorInputs(
            real01_episode_json=str(_real01_root(root) / "episode.json"),
            episode_a_sources=(str(a_source),),
            episode_c_sources=(str(c_source),),
        )
    )
    checks = {check.episode_class: check for check in decision.checks}
    assert all(check.status == "confirmed" for check in checks.values())
    assert all(check.fallback_proposal is None for check in checks.values())
    assert checks["a_talking_broll"].sources == (str(a_source),)
    assert checks["c_mixed"].sources == (str(c_source),)


def test_footage_decision_missing_real01_proposes_provisioning(tmp_path: Path) -> None:
    decision = check_footage_sources(
        FootageOperatorInputs(real01_episode_json=str(tmp_path / "real-01" / "episode.json"))
    )
    checks = {check.episode_class: check for check in decision.checks}
    assert checks["b_visual_first"].status == "fallback_proposed"
    provision = checks["b_visual_first"].fallback_proposal
    assert provision is not None
    assert "real-01" in provision


# ------------------------------------------------------------------
# (e) round-trips (strict models with tuple/dict fields survive JSON)
# ------------------------------------------------------------------


def test_round_trip_all_models(tmp_path: Path) -> None:
    report = _collect_full(tmp_path)
    assert EpisodeRunReportV1.model_validate(report.model_dump(mode="json")) == report
    summary = aggregate_three_episodes(_three_episode_reports(tmp_path))
    assert Phase6SummaryV1.model_validate(summary.model_dump(mode="json")) == summary
    decision = check_footage_sources(FootageOperatorInputs())
    assert FootageDecisionV1.model_validate(decision.model_dump(mode="json")) == decision
    # stale_state: collecting twice from the same root yields identical canonical bytes
    root = _full_episode_root(tmp_path)
    first = collect_episode_run(root, episode_class="a_talking_broll")
    second = collect_episode_run(root, episode_class="a_talking_broll")
    assert first.model_dump_json() == second.model_dump_json()


# -------------------------------------------------- CLI surface
# ------------------------------------------------------------------


def test_cli_help_lists_subcommands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "services.cli.episode_report", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    for subcommand in ("collect", "aggregate", "check-footage"):
        assert subcommand in result.stdout


def test_cli_collect_writes_report_and_aggregate_reads_it(tmp_path: Path, capsys) -> None:
    root = _full_episode_root(tmp_path)
    operator_json = tmp_path / "operator.json"
    operator_json.write_text(
        _full_operator_measurements().model_dump_json(exclude_none=True), encoding="utf-8"
    )
    code = episode_report_main(
        [
            "collect",
            "--episode-root",
            str(root),
            "--class",
            "a_talking_broll",
            "--operator-json",
            str(operator_json),
        ]
    )
    assert code == 0
    out_path = root / "episode-run-report.json"
    assert out_path.is_file()
    collected = EpisodeRunReportV1.model_validate(json.loads(out_path.read_text(encoding="utf-8")))
    assert collected.field_coverage.filled == 32
    # our own output must not be re-ingested as evidence on a second collect
    again = episode_report_main(
        ["collect", "--episode-root", str(root), "--class", "a_talking_broll"]
    )
    assert again == 0
    reparsed = EpisodeRunReportV1.model_validate(json.loads(out_path.read_text(encoding="utf-8")))
    assert "episode-run-report.json" not in reparsed.evidence_refs

    reports = _three_episode_reports(tmp_path)
    report_paths = []
    for index, report in enumerate(reports):
        path = tmp_path / f"report-{index}.json"
        path.write_text(report.model_dump_json(), encoding="utf-8")
        report_paths.append(str(path))
    summary_out = tmp_path / "summary.json"
    argv = ["aggregate"]
    for report_path in report_paths:
        argv.extend(["--report", report_path])
    argv.extend(["--out", str(summary_out)])
    code = episode_report_main(argv)
    assert code == 0
    summary = Phase6SummaryV1.model_validate(json.loads(summary_out.read_text(encoding="utf-8")))
    assert summary.non_talking_head_failure_zero is True


def test_cli_collect_missing_root_fails_typed(tmp_path: Path, capsys) -> None:
    code = episode_report_main(
        ["collect", "--episode-root", str(tmp_path / "ghost"), "--class", "a_talking_broll"]
    )
    assert code == 1
    assert "collect_failed" in capsys.readouterr().err


def test_cli_check_footage_prints_decision(tmp_path: Path, capsys) -> None:
    code = episode_report_main(
        [
            "check-footage",
            "--real01-episode-json",
            str(_real01_root(tmp_path) / "episode.json"),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    decision = FootageDecisionV1.model_validate(payload)
    assert decision.wait_policy == "no_indefinite_wait"
