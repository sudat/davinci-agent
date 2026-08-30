"""The full Phase-2 Finalization Gate LIVE test (Todo 54).

One session-scoped gate evaluation drives the REAL five-fixture chain —
offline typed-fault compile probes plus three live Resolve builds (interrupt
restart, false-complete refusal, privacy-blocked QC) with real renders —
recomputes all five frozen criteria from raw evidence, and must PASS with a
truthful phase-2 exit marker: fixture Final records stay ``fixture_only``
and are never publication decisions. Resolve is left running; only owned
``__fvp_test__`` projects are touched and swept.
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import shutil
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.gates import GateResult
from services.gates.phase2 import PHASE_2_CRITERIA, PHASE_2_FIXTURES
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p2_models import EXIT_MARKER_NAME, RESULT_NAME
from services.job_runner.gate_phase2 import evaluate, load_policy, load_receipt
from services.resolve_bridge.connection import BridgeConnectionError
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.lifecycle import project_names
from services.resolve_bridge.readiness import load_host_report

pytestmark = pytest.mark.resolve_live

#: Process locale captured at import (before any live Resolve native code
#: loads). The Resolve bridge's native module flips the process C locale to
#: ``C``/US-ASCII; the gate fixture must restore this exact state.
IMPORTED_LOCALE = locale.setlocale(locale.LC_ALL)
IMPORTED_PREFERRED_ENCODING = locale.getpreferredencoding(do_setlocale=False)

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
POLICY = Path("config/gates/phase-2-v4.json")
FREEZE_RECEIPT = ATTEMPT / "gate-cascade/receipts/phase-2-v4.json"
PARENTS = ("phase-0a", "phase-0b", "phase-1-technical")


def _host_report() -> Path:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    return ATTEMPT / "resolve-host.json"


@pytest.fixture(scope="session")
def gate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # The Resolve native bridge module sets the process C locale to "C"
    # (preferred encoding US-ASCII) when it loads — leaking that state
    # poisons every later text decode in the suite (pathlib.read_text,
    # subprocess text mode). Snapshot here, restore on EVERY exit path.
    saved_locale = locale.setlocale(locale.LC_ALL)
    try:
        if not _host_report().is_file():
            pytest.skip(
                "live requires RESOLVE_HOST_REPORT or a resolve-host.json beside the attempt"
            )
        report = load_host_report(_host_report())
        try:
            connection = launch_and_connect(report)
        except BridgeConnectionError as error:
            pytest.skip(f"live Resolve bridge unavailable and not launchable: {error}")
        root = tmp_path_factory.mktemp("phase2-gate")
        for relative in PARENTS:
            target = root / relative
            target.mkdir()
            shutil.copyfile(ATTEMPT / relative / "gate-result.json", target / "gate-result.json")
        evidence = root / "phase-2"
        policy, policy_sha256 = load_policy(POLICY)
        outcome = evaluate(
            policy,
            policy_sha256,
            evidence,
            freeze_receipt=load_receipt(FREEZE_RECEIPT, policy_sha256),
            connection=connection,
        )
        assert outcome.result.passed, [str(m) for m in outcome.mismatches]
        manager = connection.project_manager()
        owned = [name for name in project_names(manager) if name.startswith("__fvp_test__")]
        assert owned == [], f"gate must sweep every owned staging project: {owned}"
        return evidence
    finally:
        locale.setlocale(locale.LC_ALL, saved_locale)


def test_00_all_five_criteria_pass(gate: Path) -> None:
    result = GateResult.model_validate_json((gate / RESULT_NAME).read_bytes())
    assert (gate / RESULT_NAME).read_bytes() == canonical_gate_bytes(result)
    assert tuple(row.criterion_id for row in result.criteria_results) == PHASE_2_CRITERIA
    assert all(row.passed for row in result.criteria_results)
    assert result.gate_id == "phase-2"
    assert result.gate_version == "v4"


def test_10_every_criterion_is_bound_to_real_evidence(gate: Path) -> None:
    result = GateResult.model_validate_json((gate / RESULT_NAME).read_bytes())
    for row in result.criteria_results:
        assert len(row.raw_evidence_sha256s) >= 1
        assert list(row.raw_evidence_sha256s) != ["0" * 64], row.criterion_id


def test_20_exit_marker_labels_fixture_records_truthfully(gate: Path) -> None:
    marker = json.loads((gate / EXIT_MARKER_NAME).read_bytes())
    assert marker["next_checkpoint"] == "PHASE_2_EXIT"
    assert marker["fixture_records_are_publication_decisions"] is False
    assert marker["fixture_final_records"] == "fixture_only"
    assert marker["manual_resolve_ui_used"] == "none"
    assert tuple(sorted(entry["fixture_id"] for entry in marker["fixtures"])) == tuple(
        sorted(PHASE_2_FIXTURES)
    )


def test_30_clean_path_binds_real_render_and_approval(gate: Path) -> None:
    marker = json.loads((gate / EXIT_MARKER_NAME).read_bytes())
    partial = next(
        entry for entry in marker["fixtures"] if entry["fixture_id"] == "p2-partial-build-restart"
    )
    build = json.loads(Path(partial["build_output_path"]).read_bytes())
    assert build["render"]["completion_percentage"] == 100
    assert sha256_file(Path(build["render"]["output_path"])) == build["render"]["output_sha256"]
    approval = json.loads(Path(partial["approval_path"]).read_bytes())
    assert approval["target_set_hash"]
    ledger = (Path(partial["approval_path"]).parent / "final-review-events.jsonl").read_text()
    assert "approval-recorded" in ledger


def test_40_privacy_fixture_blocks_publish_and_routes_to_human(gate: Path) -> None:
    marker = json.loads((gate / EXIT_MARKER_NAME).read_bytes())
    privacy = next(
        entry for entry in marker["fixtures"] if entry["fixture_id"] == "p2-blocking-qc-privacy"
    )
    refusal = json.loads(Path(privacy["approval_refusal_path"]).read_bytes())
    assert refusal["code"] == "privacy-unresolved"
    route = json.loads(Path(privacy["human_route_path"]).read_bytes())
    assert route["human_route"] == "privacy-dismissal-required"
    assert route["fixture_only"] is True


def test_50_render_bytes_back_every_verdict(gate: Path) -> None:
    for fixture_dir in sorted((gate / "fixtures").iterdir()):
        observation = json.loads((fixture_dir / "observation.json").read_bytes())
        render_field = observation["fields"].get("render_output")
        if render_field:
            assert sha256_file(Path(render_field)) == observation["fields"]["render_sha256"]


def test_60_gate_result_binds_the_evidence_tree(gate: Path) -> None:
    result = GateResult.model_validate_json((gate / RESULT_NAME).read_bytes())
    inventory = {
        path.relative_to(gate).as_posix(): sha256_file(path)
        for path in sorted(gate.rglob("*"))
        if path.is_file() and path.name != RESULT_NAME
    }
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    assert result.evidence_bundles[0].bundle_sha256 == hashlib.sha256(payload).hexdigest()


def test_70_live_gate_leaves_process_locale_unchanged(gate: Path) -> None:
    assert locale.setlocale(locale.LC_ALL) == IMPORTED_LOCALE
    assert locale.getpreferredencoding(do_setlocale=False) == IMPORTED_PREFERRED_ENCODING
