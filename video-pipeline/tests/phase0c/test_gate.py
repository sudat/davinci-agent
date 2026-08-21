"""Offline unit tests for the Phase-0C gate logic (todo-31, fast-fake path).

Happy paths drive the fake evidence trees (pure computation, real store /
translator replay / reducer, synthetic previews — no ffmpeg) through the real
evaluator; failure paths inject the five canonical faults (wrong operator
decision, auto-applied ambiguous input, changed unrelated item, stale event,
Resolve-process requirement) and require the evaluator to reject or stop. The
FULL gate run with real ffmpeg previews lives in the acceptance command, not
in this suite.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.gates import GatePolicy
from services.gates.phase0c import PHASE_0C_CRITERIA
from services.review_command.reducer import version_ir
from services.spike.gate_phase0c_bindings import preview_ir
from services.spike.gate_phase0c_case import base_plan, load_case
from services.spike.gate_phase0c_evaluate import (
    Evaluate0cInputs,
    Evaluate0cOutcome,
    GoldenLoadError,
    evaluate0c,
    load_golden0c,
)
from services.spike.gate_phase0c_fakes import FaultKnobs0c, synthesize0c
from services.spike.gate_phase0c_models import (
    CODE_AMBIGUOUS_AUTO_APPLIED,
    CODE_GOLDEN_PLAN,
    CRITERION_AMBIGUOUS,
    CRITERION_CLEAR,
    CRITERION_CONFLICT,
    CRITERION_REBUILD,
    PHASE_0C_CASES,
    STOP_STALE_BINDING,
    STOP_UNCLASSIFIED,
)
from services.spike.stop_rules import record_stop, stop_marker_path, stop_recorded

POLICY = Path("config/gates/phase-0c-v2.json")
LOCK = Path("config/toolchains/phase-0c-v1.json")


def _parent_result() -> Path:
    override = os.environ.get("PHASE0B_RESULT")
    if override:
        return Path(override)
    lock = json.loads(LOCK.read_bytes())
    attempt_root = Path(lock["ffmpeg"]["ffmpeg"]["path"]).parents[3]
    candidate = attempt_root / "phase-0b" / "gate-result.json"
    if not candidate.is_file():
        pytest.skip(f"frozen phase-0b gate result not bootstrapped: {candidate}")
    return candidate


def _evaluate(tmp_path: Path, knobs: FaultKnobs0c) -> Evaluate0cOutcome:
    policy = GatePolicy.model_validate_json(POLICY.read_bytes())
    evidence = tmp_path / "phase-0c"
    synthesize0c(evidence, knobs)
    return evaluate0c(
        Evaluate0cInputs(
            policy=policy,
            policy_sha256=sha256_file(POLICY),
            evidence=evidence,
            parent_result_path=_parent_result(),
        )
    )


def test_happy_fake_tree_passes_all_criteria_and_every_case(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0c())
    assert outcome.result.passed is True
    assert outcome.stop_triggered is False
    assert outcome.cases_passed == PHASE_0C_CASES
    assert tuple(row.criterion_id for row in outcome.result.criteria_results) == (
        PHASE_0C_CRITERIA
    )
    assert outcome.result.policy_sha256 == sha256_file(POLICY)
    assert (tmp_path / "phase-0c" / "runs" / "p0c-remove-clear" / "rebuild.json").is_file()


def test_wrong_decision_fails_on_golden_comparison(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0c(wrong_decision=True))
    assert outcome.result.passed is False
    assert CODE_GOLDEN_PLAN in {row.code for row in outcome.mismatches}


def test_auto_applied_ambiguous_fails(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0c(auto_applied_ambiguous=True))
    assert outcome.result.passed is False
    assert CODE_AMBIGUOUS_AUTO_APPLIED in {row.code for row in outcome.mismatches}
    by_id = {row.criterion_id: row.passed for row in outcome.result.criteria_results}
    assert by_id["phase-0c-ambiguous-auto-apply-zero"] is False


def test_changed_unrelated_item_detected_via_golden(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0c(changed_unrelated_item=True))
    assert outcome.result.passed is False
    codes = {row.code for row in outcome.mismatches}
    assert "golden-plan-mismatch" in codes
    assert any(
        "store head plan does not match the deterministic replay" in row.detail
        for row in outcome.mismatches
    )


def test_stale_event_stops_and_marker_refuses_rerun(tmp_path: Path) -> None:
    outcome = _evaluate(tmp_path, FaultKnobs0c(stale_event=True))
    assert outcome.result.passed is False
    assert outcome.stop_triggered is True
    assert outcome.stop_criterion == STOP_STALE_BINDING
    marker = record_stop(
        tmp_path / "phase-0c",
        outcome.stop_criterion,
        outcome.stop_reason,
        gate_id="phase-0c",
    )
    assert marker == stop_marker_path(tmp_path / "phase-0c")
    recorded = stop_recorded(tmp_path / "phase-0c")
    assert recorded is not None
    assert recorded.gate_id == "phase-0c"


def test_malformed_evidence_tree_stops_unclassified(tmp_path: Path) -> None:
    evidence = tmp_path / "phase-0c"
    synthesize0c(evidence, FaultKnobs0c())
    (evidence / "runs" / "p0c-locked-conflict" / "translator-record.json").unlink()
    policy = GatePolicy.model_validate_json(POLICY.read_bytes())
    outcome = evaluate0c(
        Evaluate0cInputs(
            policy=policy,
            policy_sha256=sha256_file(POLICY),
            evidence=evidence,
            parent_result_path=_parent_result(),
        )
    )
    assert outcome.stop_triggered is True
    assert outcome.stop_criterion == STOP_UNCLASSIFIED
    assert outcome.result.passed is False


def test_golden_drift_against_policy_is_rejected() -> None:
    policy = GatePolicy.model_validate_json(POLICY.read_bytes())
    drifted = policy.model_copy(update={"golden_sha256": "0" * 64})
    with pytest.raises(GoldenLoadError):
        load_golden0c(drifted)


def test_preview_ir_adds_linked_audio_only_when_missing() -> None:

    ambiguous = base_plan(load_case("p0c-ambiguous-two-targets"))
    projected = preview_ir(version_ir(ambiguous, ambiguous, 1))
    kinds = tuple(track.track.kind for track in projected.tracks)
    assert kinds == ("video", "subtitle", "audio")
    audio = projected.tracks[-1]
    video = projected.tracks[0]
    assert [item.record_span for item in audio.items] == [
        item.record_span for item in video.items
    ]
    clear = base_plan(load_case("p0c-remove-clear"))
    assert preview_ir(version_ir(clear, clear, 1)).tracks == version_ir(clear, clear, 1).tracks


def test_gate0c_modules_never_import_resolve_bridge() -> None:
    for path in sorted(Path("services/spike").glob("gate_phase0c*.py")):
        tree = ast.parse(path.read_bytes())
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.add(node.module)
        offenders = {module for module in modules if "resolve_bridge" in module}
        assert not offenders, f"{path.name} imports resolve_bridge: {offenders}"


def test_gate0c_import_keeps_resolve_bridge_out_of_sys_modules() -> None:
    modules = sorted(Path("services/spike").glob("gate_phase0c*.py"))
    names = [f"services.spike.{path.stem}" for path in modules]
    code = (
        "import sys\n"
        + "".join(f"import {name}\n" for name in names)
        + "leaked = [m for m in sys.modules if m.startswith('services.resolve_bridge')]\n"
        "print('resolve-modules=' + (','.join(leaked) or 'none'))\n"
        "raise SystemExit(1 if leaked else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resolve-modules=none" in result.stdout


def test_criterion_constants_are_the_frozen_four() -> None:
    assert (CRITERION_CLEAR, CRITERION_AMBIGUOUS, CRITERION_CONFLICT, CRITERION_REBUILD) == (
        PHASE_0C_CRITERIA
    )


def test_defer_cases_leave_preview_1_absent(tmp_path: Path) -> None:
    _evaluate(tmp_path, FaultKnobs0c())
    for fixture_id in ("p0c-ambiguous-two-targets", "p0c-locked-conflict"):
        run = tmp_path / "phase-0c" / "runs" / fixture_id
        assert (run / "preview-0" / "preview-trace.json").is_file()
        assert not (run / "preview-1").exists()
        assert not (run / "store" / "plan-v2.json").exists()


def test_clear_cases_produce_plan_v2_and_preview_1(tmp_path: Path) -> None:
    _evaluate(tmp_path, FaultKnobs0c())
    for fixture_id in ("p0c-remove-clear", "p0c-span-clear", "p0c-subtitle-clear"):
        run = tmp_path / "phase-0c" / "runs" / fixture_id
        assert (run / "store" / "plan-v2.json").is_file()
        assert (run / "store" / "ir-v2.json").is_file()
        assert (run / "preview-1" / "preview-trace.json").is_file()
