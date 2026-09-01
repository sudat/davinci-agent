"""Todo-62 acceptance: the Phase-3 A/B Profile-swap Gate (offline units).

The frozen policy binds one phase-2 parent and the four criteria; the
offline A/B compile proves the SAME frozen editorial structure with
presentation differing exactly along the declared golden dimensions; the
REAL evaluator over the synthesized evidence tree PASSES the baseline and
detects every typed fault (structure drift, one-profile-only, channel
branch, stale asset, manual fallback, rights issue, Phase-4 import);
malformed or noncanonical policies exit with usage errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.job_runner import gate_p3_faults
from services.job_runner.gate_p3_ab import (
    compile_ab,
    presentation_diff,
    structure_drift,
    structure_rows,
)
from services.job_runner.gate_p3_fake_tree import (
    EXPECTED_CODES,
    FAULTS,
    FaultKnobs,
    synthesize_evidence,
)
from services.job_runner.gate_p3_models import P3ScanRecord
from services.job_runner.gate_p3_scan import (
    default_channel_roots,
    scan_channel_branches,
    scan_phase4_imports,
)
from services.job_runner.gate_phase3 import evaluate, parser, run_phase3_gate

POLICY = Path("config/gates/phase-3-v3.json")


@pytest.fixture(scope="module")
def context() -> gate_p3_faults._FaultContext:
    return gate_p3_faults._build_context(POLICY)


def test_baseline_synthetic_tree_passes_every_criterion(
    context: gate_p3_faults._FaultContext, tmp_path: Path
) -> None:
    outcome = gate_p3_faults._evaluate_fake(tmp_path, FaultKnobs(), context)
    assert outcome.result.passed is True
    assert outcome.mismatches == ()
    assert [row.passed for row in outcome.result.criteria_results] == [True] * 4


@pytest.mark.parametrize("fault", FAULTS)
def test_every_typed_fault_is_detected(
    context: gate_p3_faults._FaultContext, tmp_path: Path, fault: str
) -> None:
    outcome = gate_p3_faults._evaluate_fake(tmp_path, FaultKnobs(fault=fault), context)
    codes = [code for code, _detail in outcome.mismatches]
    assert outcome.result.passed is False
    assert EXPECTED_CODES[fault] in codes


def test_regression_failure_is_typed(
    context: gate_p3_faults._FaultContext, tmp_path: Path
) -> None:
    gate_p3_faults._write_parent(tmp_path, context.parent)
    tree = synthesize_evidence(tmp_path, FaultKnobs())
    result_path = Path(tree.observation.regression.result_path)
    payload = json.loads(result_path.read_bytes())
    payload["criteria_results"][0]["passed"] = False
    payload["passed"] = False
    result_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    outcome = evaluate(
        context.policy,
        context.policy_sha256,
        tree.evidence,
        drive=False,
        observation=tree.observation,
    )
    codes = [code for code, _detail in outcome.mismatches]
    assert outcome.result.passed is False
    assert "phase2-regression-failed" in codes


def test_ab_compile_structure_is_identical_and_presentation_declared() -> None:
    plan = compile_ab()
    manifest_a = plan.manifests["p3-brand-a"]
    manifest_b = plan.manifests["p3-brand-b"]
    assert manifest_a.manifest_sha256 != manifest_b.manifest_sha256
    observed, declared = presentation_diff(manifest_a, manifest_b)
    assert observed == declared
    assert len(observed) == 19
    rows_a = structure_rows(plan.timeline_ir)
    rows_b = tuple(
        row.model_copy() for row in structure_rows(plan.timeline_ir)
    )
    assert structure_drift(rows_a, rows_b) == ()
    drifted = (rows_b[0].model_copy(update={"record_end": rows_b[0].record_end + 1}), *rows_b[1:])
    assert structure_drift(rows_a, drifted)


def test_channel_scan_is_clean_on_the_declared_builder_surface() -> None:
    assert scan_channel_branches(default_channel_roots()) == ()


def test_channel_scan_detects_a_planted_branch(tmp_path: Path) -> None:
    planted = tmp_path / "planted_builder.py"
    planted.write_bytes(
        b"def render(channel_id: str) -> int:\n"
        b"    if channel_id == 'brand-b':\n"
        b"        return 1\n"
        b"    return 0\n"
    )
    findings = scan_channel_branches((planted,))
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "channel_id" in findings[0].snippet


def test_channel_scan_flags_an_incomplete_scope(
    context: gate_p3_faults._FaultContext, tmp_path: Path
) -> None:
    gate_p3_faults._write_parent(tmp_path, context.parent)
    tree = synthesize_evidence(tmp_path, FaultKnobs())
    scan = P3ScanRecord(channel_roots=(), channel_findings=(), phase4_modules=())
    observation = tree.observation.model_copy(update={"scan": scan})
    outcome = evaluate(
        context.policy,
        context.policy_sha256,
        tree.evidence,
        drive=False,
        observation=observation,
    )
    codes = [code for code, _detail in outcome.mismatches]
    assert "channel-scan-incomplete" in codes
    assert outcome.result.passed is False


def test_phase4_scan_flags_capability_module_names() -> None:
    assert scan_phase4_imports(("services.travel.scene_detection", "services.editorial.core")) == (
        "services.travel.scene_detection",
    )


def test_missing_policy_exits_usage(tmp_path: Path) -> None:
    arguments = parser().parse_args(
        ["--policy", str(tmp_path / "absent.json"), "--evidence", str(tmp_path / "ev")]
    )
    assert run_phase3_gate(arguments) == 2


def test_noncanonical_policy_exits_usage(tmp_path: Path) -> None:
    payload = json.loads(POLICY.read_bytes())
    edited = tmp_path / "policy.json"
    edited.write_text(json.dumps(payload, indent=1))
    arguments = parser().parse_args(
        ["--policy", str(edited), "--evidence", str(tmp_path / "ev")]
    )
    assert run_phase3_gate(arguments) == 2
