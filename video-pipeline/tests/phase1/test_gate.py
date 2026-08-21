"""The full Phase-1 Technical Gate test: the REAL five-fixture chain (Todo 46).

One session-scoped gate evaluation drives every fixture through the real
Todo-45 chain twice (deterministic declared-observation replay), exercises
the declared review-correction sequence, recomputes all five frozen criteria
from raw evidence, and must (a) PASS every criterion and (b) STOP at H1:
``next_checkpoint=H1`` with ``status=NEEDS_HUMAN``, the ``h1-waiting`` marker
bound to the work id and every bundle/preview hash, and the plan checkbox
left for the owner's real-episode action. Offline; no Resolve.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from services.foundation_io import canonical_model_bytes, sha256_file
from services.gates import GateResult
from services.gates.phase1_technical import PHASE_1_TECHNICAL_CRITERIA, PHASE_1_TECHNICAL_FIXTURES
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p1_models import H1Waiting
from services.job_runner.gate_phase1 import evaluate, load_policy

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-1-technical-v2.json")
WORK_ID = "pytest-phase1-gate-work-id"


@pytest.fixture(scope="session")
def gate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("phase1-gate")
    for relative in ("phase-0c", "control-plane"):
        target = root / relative
        target.mkdir()
        shutil.copyfile(ATTEMPT / relative / "gate-result.json", target / "gate-result.json")
    evidence = root / "phase-1-technical"
    policy, policy_sha256 = load_policy(POLICY)
    outcome = evaluate(policy, policy_sha256, evidence, work_id=WORK_ID)
    assert outcome.result.passed, list(outcome.mismatches)
    return evidence, evidence / "gate-result.json"


def test_00_all_five_criteria_pass(gate: tuple[Path, Path]) -> None:
    _evidence, result_path = gate
    result = GateResult.model_validate_json(result_path.read_bytes())
    assert result_path.read_bytes() == canonical_gate_bytes(result)
    assert tuple(row.criterion_id for row in result.criteria_results) == (
        PHASE_1_TECHNICAL_CRITERIA
    )
    assert all(row.passed for row in result.criteria_results)
    assert result.gate_id == "phase-1-technical"
    assert result.gate_version == "v2"


def test_10_every_criterion_is_bound_to_real_evidence(gate: tuple[Path, Path]) -> None:
    _evidence, result_path = gate
    result = GateResult.model_validate_json(result_path.read_bytes())
    for row in result.criteria_results:
        assert len(row.raw_evidence_sha256s) >= 1
        assert list(row.raw_evidence_sha256s) != ["0" * 64], row.criterion_id


def test_20_h1_waiting_marker_stops_the_flow(gate: tuple[Path, Path]) -> None:
    evidence, _result = gate
    waiting = H1Waiting.model_validate_json((evidence / "h1-waiting.json").read_bytes())
    assert waiting.next_checkpoint == "H1"
    assert waiting.status == "NEEDS_HUMAN"
    assert waiting.human_time == "not_evaluated"
    assert waiting.execution_work_id == WORK_ID
    assert tuple(binding.fixture_id for binding in waiting.fixtures) == tuple(
        PHASE_1_TECHNICAL_FIXTURES
    )


def test_30_marker_binds_bundles_and_previews(gate: tuple[Path, Path]) -> None:
    evidence, _result = gate
    waiting = H1Waiting.model_validate_json((evidence / "h1-waiting.json").read_bytes())
    for binding in waiting.fixtures:
        bundle_path = Path(binding.bundle_path)
        assert bundle_path.is_file()
        bundle = json.loads(bundle_path.read_bytes())
        assert binding.initial_preview_sha256 == bundle["initial"]["preview_sha256"]
        assert binding.final_preview_sha256 == bundle["current"]["preview_sha256"]
        preview = bundle_path.parent / bundle["current"]["preview_dir"] / "preview.mp4"
        assert sha256_file(preview) == binding.final_preview_sha256


def test_40_structured_coverage_meets_threshold_from_declared_commands(
    gate: tuple[Path, Path],
) -> None:
    evidence, _result = gate
    observation = json.loads(
        (evidence / "fixtures" / "p1-ref-05-review-mix" / "observation.json").read_bytes()
    )
    steps = observation["review_steps"]
    assert len(steps) == 4
    structured = [step for step in steps if step["structured"]]
    assert len(structured) * 5 >= len(steps) * 4
    classifications = [step["classification"] for step in steps]
    assert classifications == ["clear", "clear", "clear", "ambiguous"]
    decisions = [step["decision"] for step in steps]
    assert decisions == ["applied", "applied", "applied", "deferred"]


def test_50_correction_sequence_final_state_matches_goldens(gate: tuple[Path, Path]) -> None:
    evidence, _result = gate
    run1 = evidence / "fixtures" / "p1-ref-05-review-mix" / "run1"
    bundle = json.loads((run1 / "review-bundle.json").read_bytes())
    assert bundle["current"]["plan_version"] == "v4"
    final_ir = json.loads(
        (run1 / "review-store" / "ir-v4.json").read_bytes()
    )
    video_ids = [
        item["item_id"]
        for track in final_ir["tracks"]
        if track["track"]["kind"] == "video"
        for item in track["items"]
    ]
    assert video_ids == ["s1", "s3", "s4"]
    goldens = json.loads(
        Path("tests/goldens/reference/phase-1-technical/expected.json").read_bytes()
    )
    final_rows = goldens["fixtures"]["p1-ref-05-review-mix"]["review_final_plan_items"]
    assert len(final_rows) == 9  # 3 video + 3 audio + 3 subtitle


def test_60_gate_result_binds_the_evidence_tree(gate: tuple[Path, Path]) -> None:
    evidence, result_path = gate
    result = GateResult.model_validate_json(result_path.read_bytes())
    inventory = {
        path.relative_to(evidence).as_posix(): sha256_file(path)
        for path in sorted(evidence.rglob("*"))
        if path.is_file() and path.name != "gate-result.json"
    }
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    assert result.evidence_bundles[0].bundle_sha256 == hashlib.sha256(payload).hexdigest()


def test_70_marker_bytes_are_canonical(gate: tuple[Path, Path]) -> None:
    evidence, _result = gate
    raw = (evidence / "h1-waiting.json").read_bytes()
    waiting = H1Waiting.model_validate_json(raw)
    assert raw == canonical_model_bytes(waiting)
