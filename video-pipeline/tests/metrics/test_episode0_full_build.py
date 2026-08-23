"""Episode-0 FULL BUILD rerun harness (task 43) — TDD, synthetic data only.

Proves the ``episode0 run --phase full-build`` machinery end-to-end on the
task-28 synthetic W3 episode with the FAKE executor (NO real footage, NO
LLM, NO live Resolve — the operator's real run is a later invocation):

(a) baseline present → synthetic full-build run completes and writes every
    build artifact + report.json + gate-check.json (V43-2, editorial
    stages) + gate-check-v43-3.json (V43-3, all 13 criteria pass);
(b) one blocked quality domain → the V43-3 gate FAILS naming that domain;
(c) the legacy rollback leg: run-events contain execution_backend
    mcp→legacy_direct→mcp transitions (then the final restore);
(d) ``--executor live`` with an unreachable server → typed BLOCKED
    escalation (never a crash), flags untouched;
(e) determinism: two synthetic runs into separate roots produce identical
    canonical report bytes after root/run normalization;
(f) missing baseline → BLOCKED (Gate V43-0.5 precondition, T30 semantics).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.cli.episode0 import main
from services.foundation_io import atomic_write, canonical_model_bytes
from services.mcp_client.errors import McpClientError
from services.metrics.episode0_gate_v43_3 import (
    Episode0FullBuildReportV1,
    FullBuildGateEvidenceV1,
    check_gate,
)
from tests.editorial_v2.fixtures.three_pass_fixture import make_brief
from tests.metrics.test_episode0_rerun import (
    _HEURISTIC_RUNTIME,
    _preview_compatible_artifact,
    _write_backends,
    _write_baseline,
)

GATE_REQUIREMENTS = {
    "ir-v2-compiles-to-mcp-execution",
    "subtitles-usable-when-required",
    "audio-plan-executed-checked",
    "color-plan-or-justified-noop",
    "broll-primary-coexist",
    "seven-domains-explicit",
    "recipes-resolved-via-kit",
    "recipe-provenance-recorded",
    "no-blocked-domain-at-final",
    "readback-source-record-verified",
    "render-qc-completes",
    "publishability-review-recorded",
    "legacy-rollback-available",
}

BUILD_ARTIFACTS = (
    "presentation-intents.json",
    "subtitle-plan.json",
    "audio-plan.json",
    "color-plan.json",
    "kit-selections.json",
    "mcp-execution-plan.json",
    "mcp-run-report.json",
    "render-record.json",
    "quality-domain-report.json",
    "editorial-qc-report.json",
    "timeline-ir-v2.json",
    "presentation-preview.trace.json",
    "publishability-review-input.json",
    "report.json",
    "gate-check.json",
    "gate-check-v43-3.json",
    "run-events.jsonl",
)


class _FullBuildWorkspace:
    """Synthetic full-build workspace (tmp only; real reference episodes untouched)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_root = root / "runs"
        self.brief_path = root / "brief.json"
        self.mi_path = root / "media-intelligence.json"
        # NO-LLM harness by design: the shipped default is production_model
        # (BLOCKS without credentials), so these runs opt into heuristic mode.
        self.editorial_runtime_path = root / "editorial-runtime.json"
        atomic_write(self.brief_path, canonical_model_bytes(make_brief()))
        atomic_write(self.mi_path, canonical_model_bytes(_preview_compatible_artifact()))
        atomic_write(
            self.editorial_runtime_path, canonical_model_bytes(_HEURISTIC_RUNTIME)
        )

    def argv(self, *, run_id: str, backends: Path, extra: tuple[str, ...] = ()) -> list[str]:
        return [
            "run",
            "--phase",
            "full-build",
            "--episode",
            "synthetic-01",
            "--episode-root",
            str(self.root),
            "--brief",
            str(self.brief_path),
            "--mi-artifact",
            str(self.mi_path),
            "--backends",
            str(backends),
            "--editorial-runtime",
            str(self.editorial_runtime_path),
            "--run-id",
            run_id,
            *extra,
        ]

    @property
    def run_dir(self) -> Path:
        return self.runs_root / "unit-full-build"


@pytest.fixture
def workspace(tmp_path: Path) -> _FullBuildWorkspace:
    return _FullBuildWorkspace(tmp_path)


@pytest.fixture
def backends(tmp_path: Path) -> Path:
    return _write_backends(tmp_path)


def _run_events(run_dir: Path) -> list[dict[str, object]]:
    lines = (run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _successful_run(workspace: _FullBuildWorkspace, backends: Path) -> None:
    _write_baseline(workspace.runs_root)
    rc = main(workspace.argv(run_id="unit-full-build", backends=backends))
    assert rc == 0


# ------------------------------------------- (a) synthetic run end-to-end


def test_full_build_run_generates_all_artifacts_and_passes_gate(
    workspace: _FullBuildWorkspace, backends: Path
) -> None:
    """Given: measured baseline + synthetic brief/MI artifact + fake executor;
    When: run --phase full-build; Then: every build artifact exists, the
    V43-3 gate passes all 13 criteria, operator fields stay null."""

    _successful_run(workspace, backends)
    for name in BUILD_ARTIFACTS:
        assert (workspace.run_dir / name).is_file(), name

    report = Episode0FullBuildReportV1.model_validate(
        json.loads((workspace.run_dir / "report.json").read_bytes())
    )
    assert report.phase == "full-build"
    assert report.executor == "fake"
    assert report.operator.active_human_time_minutes is None
    # editorial stages (T30 machinery) ran inside the full build
    assert report.editorial.review_correction.exercised is True
    assert report.editorial.validation.hallucination_count == 0
    # plans compiled from the editorial IR
    assert report.plans.plan_step_count >= 1
    assert report.plans.subtitle_cue_count >= 1
    assert report.plans.density_within_limits is True
    # active domain recipes resolved via the versioned kit, provenance recorded
    recipe_ids = {sel.recipe_id for sel in report.plans.kit_selections}
    assert {"subtitle/default", "audio/dialogue-chain", "color/technical-normalize"} <= (recipe_ids)
    for sel in report.plans.kit_selections:
        assert sel.origin
        assert sel.license
        assert sel.fallback
        assert sel.rationale
    # execution via the T39 runner with the fake executor
    assert report.execution.outcome == "completed"
    assert report.execution.failed_step_count == 0
    assert report.execution.readback_matched_step_count == report.execution.step_count
    # seven quality domains, none blocked
    assert len(report.quality.domain_statuses) == 7
    assert report.quality.blocked_domains == ()
    assert "blocked" not in report.quality.domain_statuses.values()
    # publishability scaffold is recorded, operator-fillable (never fabricated)
    assert report.publishability.filled is False
    assert report.publishability.publishable is None
    # rollback leg executed
    assert report.rollback.checked is True
    assert report.rollback.transitions == ("mcp->legacy_direct", "legacy_direct->mcp")
    assert report.rollback.legacy_verified_backend == "legacy_direct"
    # presentation preview stub launch (T60) recorded
    assert report.preview.status == "placeholder-not-rendered"

    gate = json.loads((workspace.run_dir / "gate-check-v43-3.json").read_bytes())
    assert gate["passed"] is True
    assert {c["requirement"] for c in gate["criteria"]} == GATE_REQUIREMENTS
    assert all(c["status"] == "pass" for c in gate["criteria"])
    # the editorial (V43-2) checklist rides along for the editorial stages
    editorial_gate = json.loads((workspace.run_dir / "gate-check.json").read_bytes())
    assert editorial_gate["gate"] == "V43-2"
    assert editorial_gate["passed"] is True

    # prior editorial rerun absent → 比較不能 display-only (never a block)
    events = _run_events(workspace.run_dir)
    prior = [e for e in events if e["event"] == "prior_editorial_run"]
    assert len(prior) == 1
    assert prior[0]["found"] is False


def test_full_build_flags_restored_after_success(
    workspace: _FullBuildWorkspace, backends: Path
) -> None:
    """Given: a successful run; Then: both flags restored to their prior values."""

    _successful_run(workspace, backends)
    text = (backends).read_text(encoding="utf-8")
    assert '"legacy_direct"' in text
    assert '"phase1_v1"' in text


# ------------------------------------------- (b) blocked-domain gate probe


def test_gate_fails_when_one_domain_blocked(workspace: _FullBuildWorkspace, backends: Path) -> None:
    """Given: a poisoned report/evidence pair with audio_finishing blocked;
    Then: the V43-3 gate fails, naming audio_finishing in the failing
    criterion's detail."""

    _successful_run(workspace, backends)
    report = Episode0FullBuildReportV1.model_validate(
        json.loads((workspace.run_dir / "report.json").read_bytes())
    )
    statuses = {**report.quality.domain_statuses, "audio_finishing": "blocked"}
    poisoned = report.model_copy(
        update={
            "quality": report.quality.model_copy(
                update={
                    "domain_statuses": statuses,
                    "blocked_domains": ("audio_finishing",),
                }
            )
        }
    )
    evidence = FullBuildGateEvidenceV1(
        plan_step_count=report.plans.plan_step_count,
        subtitle_cue_count=report.plans.subtitle_cue_count,
        dialogue_present=True,
        audio_steps_executed=True,
        color_steps_executed=True,
        color_sections_needed=("technical_correction",),
        b_roll_track_present=True,
        primary_track_present=True,
        domain_statuses=statuses,
        blocked_domains=("audio_finishing",),
        recipe_selection_count=len(report.plans.kit_selections),
        provenance_complete=True,
        readback_matched_step_count=report.execution.step_count,
        run_outcome="completed",
        render_record_present=True,
        publishability_scaffold_present=True,
        publishable=None,
        rollback_transitions=report.rollback.transitions,
    )
    gate = check_gate(poisoned, evidence)
    assert gate.passed is False
    failed = [c for c in gate.criteria if c.status == "fail"]
    assert [c.requirement for c in failed] == ["no-blocked-domain-at-final"]
    assert "audio_finishing" in failed[0].detail


# ------------------------------------------- (c) rollback leg in run-events


def test_rollback_leg_logs_execution_backend_transitions(
    workspace: _FullBuildWorkspace, backends: Path
) -> None:
    """Given: a successful full-build run; Then: run-events contain the
    execution_backend mcp→legacy_direct→mcp rollback leg before the final
    restore to the prior value."""

    _successful_run(workspace, backends)
    events = _run_events(workspace.run_dir)
    exec_events = [e for e in events if e.get("key") == "execution_backend"]
    to_sequence = [e["to"] for e in exec_events if "to" in e]
    assert to_sequence == ["mcp", "legacy_direct", "mcp", "legacy_direct"]
    flip = next(e for e in exec_events if e["event"] == "rollback_flipped")
    assert flip["from"] == "mcp"
    verified = next(e for e in exec_events if e["event"] == "rollback_verified")
    assert verified["backend"] == "legacy_direct"


# ------------------------------------------- (d) live-refusal typed BLOCKED


def test_live_executor_refusal_is_typed_block_not_crash(
    workspace: _FullBuildWorkspace,
    backends: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Given: --executor live and a stubbed client whose connect() refuses;
    Then: typed BLOCKED escalation (explicit code, no traceback), non-zero
    exit, flags never switched."""

    _write_baseline(workspace.runs_root)

    class _RefusingClient:
        def connect(self) -> None:
            raise McpClientError("stub transport refusal")

    monkeypatch.setattr(
        "services.cli.episode0._live_client_from_pin", lambda _pin: _RefusingClient()
    )
    rc = main(
        workspace.argv(run_id="live-refused", backends=backends, extra=("--executor", "live"))
    )
    assert rc != 0
    stderr = capsys.readouterr().err
    assert "blocked:" in stderr
    assert "mcp-server-unreachable" in stderr
    assert "BLOCKED" in stderr
    assert "Traceback" not in stderr
    # the refusal happened before any flag switch
    assert '"legacy_direct"' in backends.read_text(encoding="utf-8")
    assert not (workspace.runs_root / "live-refused" / "report.json").is_file()


# ------------------------------------------- (e) determinism of report bytes


def test_synthetic_run_report_bytes_are_deterministic(tmp_path: Path) -> None:
    """Given: two identical synthetic runs into separate roots; Then: the
    canonical report bytes match after root-path normalization (stale-state
    probe: nothing wall-clock or order dependent leaks into the report)."""

    payloads = []
    for label in ("det-a", "det-b"):
        root = tmp_path / label
        workspace = _FullBuildWorkspace(root)
        backends = _write_backends(root)
        _write_baseline(workspace.runs_root)
        assert main(workspace.argv(run_id="unit-full-build", backends=backends)) == 0
        payload = json.loads((workspace.run_dir / "report.json").read_bytes())
        payloads.append(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            .replace(str(root), "<root>")
            .encode()
        )
    assert payloads[0] == payloads[1]


# ------------------------------------------- (f) baseline precondition


def test_full_build_blocked_when_baseline_missing(
    workspace: _FullBuildWorkspace, backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: no baseline report; When: run --phase full-build; Then: BLOCKED
    (Gate V43-0.5 unmet, T30 semantics) — the build never starts."""

    rc = main(workspace.argv(run_id="blocked-full", backends=backends))
    assert rc != 0
    stderr = capsys.readouterr().err
    assert "Gate V43-0.5" in stderr
    assert "BLOCKED" in stderr
    assert not (workspace.runs_root / "blocked-full" / "report.json").is_file()
