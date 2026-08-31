"""Execution runner + readback + fallback ladder tests (task 39).

The runner executes a committed McpExecutionPlanV1 SERIALLY under the
single-writer discipline with a FAKE executor replaying canned per-step
actuals (no live MCP in unit tests — the live smoke is task 43).

Locked behaviors:

(a) happy plan -> all steps ok -> report completed + ledger entries;
(b) readback mismatch -> step fails -> transient retry succeeds (bounded
    count asserted) -> report notes retries;
(c) permanent failure -> ladder transition recorded (parametrized rungs);
(d) fallback steps (T38 records) -> explicit rung entry, never silent;
(e) qc_aggregate summary from actuals + provider evidence refs;
(f) build-report-compatible section shape (warnings/failures rows);
(g) determinism of report canonical bytes for identical inputs;
(h) guard integration: legacy backend flag refuses mutating execution
    BEFORE any executor call, audited in the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.config.backends import BackendsConfig
from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.foundation_io import canonical_model_bytes
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import (
    McpCallRecorder,
    McpExecutionReportV1,
    read_call_records,
)
from services.mcp_client.errors import McpTimeoutError
from services.mcp_client.execution_runner import (
    BackendPolicyError,
    NotLeaseHolderError,
)
from services.mcp_execution.fallback import (
    FallbackLadderError,
    FallbackRungEntryV1,
    plan_declared_entry,
    runtime_transition,
)
from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.plan_models import (
    FallbackRung,
    FallbackStepRecordV1,
    McpExecutionPlanV1,
    McpExecutionStepV1,
    compute_plan_id,
)
from services.mcp_execution.plan_payloads import (
    AudioMetricReadback,
    AudioOpParams,
    AudioStateReadback,
    ImportMediaParams,
    ImportReadback,
    PlaceClipParams,
    PlacementReadback,
    PrepareProjectParams,
    ProjectReadback,
    SubtitleCuePayload,
    SubtitleCuesReadback,
    TransformParams,
    TransformReadback,
)
from services.mcp_execution.qc_aggregate import (
    ProviderCheckV1,
    aggregate_technical_qc,
    checks_from_run_report,
    to_qc_issue_rows,
)
from services.mcp_execution.readback import (
    to_build_report_rows,
    verify_readback,
)
from services.mcp_execution.runner import (
    McpExecutionRunnerV2,
    McpExecutionRunReportV1,
    RetryPolicy,
    UnknownStepActionError,
)
from services.mcp_execution.step_builders import mcp_or_executor, step_from

EPISODE: Identifier = "ep-run-1"
RATE = RationalFrameRate(num=30, den=1)


# ------------------------------------------------------------- fixtures


class FakeExecutor:
    """Replays canned per-step actuals keyed by action; records every call.

    A scripted value may be a single payload (returned for every call) or a
    list consumed left-to-right (one per attempt); an Exception instance is
    raised instead of returned. No server, no I/O.
    """

    def __init__(self, script: Mapping[str, object] | None = None) -> None:
        self.script: dict[str, list[object]] = {
            action: list(value) if isinstance(value, list) else [value]
            for action, value in (script or {}).items()
        }
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append((tool_name, action, dict(normalized_params)))
        queue = self.script.get(action)
        if queue is None:
            raise AssertionError(f"unexpected action dispatched: {action}")
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, Exception):
            raise value
        return value


def _write_backends(path: Path, execution_backend: str) -> Path:
    cfg = {
        "schema_version": "backends-v1",
        "execution_backend": execution_backend,
        "analysis_backend": "legacy_local",
        "editorial_contract": "phase1_v1",
    }
    BackendsConfig.model_validate(cfg)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _recorder() -> McpCallRecorder:
    return McpCallRecorder(
        provider_version="2.98.3",
        resolve_version="21.0.4",
        server_mode="test",
        clock=lambda: 0,
    )


def _make_runner(
    tmp_path: Path,
    store: StateStore,
    *,
    execution_backend: str = "mcp",
    retry_policy: RetryPolicy | None = None,
    ledger_dir: Path | None = None,
) -> McpExecutionRunnerV2:
    return McpExecutionRunnerV2(
        store=store,
        job_id="dev-job-1",
        stage_name="stage-build",
        holder_token="holder-1",  # noqa: S106 (lease token, not a credential)
        ledger_dir=ledger_dir if ledger_dir is not None else tmp_path / "ledger",
        backends_path=_write_backends(
            tmp_path / f"backends-{execution_backend}.json", execution_backend
        ),
        clock=SequenceClock(1000),
        retry_policy=retry_policy if retry_policy is not None else RetryPolicy(),
    )


def _acquire_lease(store: StateStore, holder: str = "holder-1") -> None:
    store.acquire_lease(
        resource=stage_resource("dev-job-1", "stage-build"),
        holder=holder,
        now=1000,
        ttl_seconds=60,
    )


_SRC_SPAN = SourceFrameSpan(start_frame=0, end_frame=90, rate=RATE)
_REC_SPAN = RecordFrameSpan(start_frame=0, end_frame=90)


def _prepare_step() -> McpExecutionStepV1:
    return step_from(
        "stp-prepare-ep-run-1",
        PrepareProjectParams(
            action="prepare_project", timeline_name="ep-run-1-timeline", fps_num=30, fps_den=1
        ),
        ProjectReadback(kind="project", project_name="ep-run-1-timeline", timeline_frame_rate="30"),
        "prepare_project",
        "mcp_verified_workflow",
        None,
        -2,
    )


def _import_step(source_id: str = "src-cam-a") -> McpExecutionStepV1:
    return step_from(
        f"stp-import-{source_id}",
        ImportMediaParams(action="import_media", source_id=source_id),
        ImportReadback(kind="import", source_id=source_id),
        "safe_import_media",
        "mcp_verified_workflow",
        None,
        -1,
        project_ready=True,
    )


def _place_step(
    item_id: str = "itm-1",
    source_id: str = "src-cam-a",
    rung: FallbackRung = "mcp_verified_workflow",
) -> McpExecutionStepV1:
    return step_from(
        f"stp-place-{item_id}",
        PlaceClipParams(
            action="place_clip",
            item_id=item_id,
            source=SourceRef(source_id=source_id, span=_SRC_SPAN),
            record_span=_REC_SPAN,
            track_role="primary",
        ),
        PlacementReadback(
            kind="placement", item_id=item_id, source_span=_SRC_SPAN, record_span=_REC_SPAN
        ),
        mcp_or_executor(rung, "append_to_timeline"),
        rung,
        None,
        0,
        project_ready=True,
        timeline_ready=True,
        media=(source_id,),
    )


def _transform_step(rung: FallbackRung) -> McpExecutionStepV1:
    # T38 parity: rungs outside verified/granular MCP REQUIRE a fallback record
    record = (
        None
        if rung in ("mcp_verified_workflow", "mcp_granular_tool")
        else FallbackStepRecordV1(rung=rung, reason=f"test fixture: transform placed on {rung}")
    )
    return step_from(
        "stp-transform-itm-1",
        TransformParams(
            action="apply_transform", effect_kind="punch_in", target_item_id="itm-1", note=""
        ),
        TransformReadback(kind="transform", item_id="itm-1", properties=("zoom_x",)),
        mcp_or_executor(rung, "set_transform"),
        rung,
        record,
        500,
        project_ready=True,
        timeline_ready=True,
    )


def _eq_fallback_step() -> McpExecutionStepV1:
    record = FallbackStepRecordV1(
        rung="direct_scripting_gap_adapter",
        reason=(
            "capability 'audio-property-operation' status 'failed' "
            "(matrix fallback 'legacy_direct') for eq"
        ),
        capability="audio-property-operation",
        status="failed",
    )
    return step_from(
        "stp-eq-stage",
        AudioOpParams(
            action="apply_audio_op",
            effect_kind="eq",
            stage="optional_eq_compression_voice_isolation",
            capability="audio-property-operation",
            justification="room resonance needs correction",
        ),
        AudioStateReadback(kind="audio_state", item_ref="timeline", state_property="eq"),
        "direct_script_adapter",
        "direct_scripting_gap_adapter",
        record,
        600,
        project_ready=True,
        timeline_ready=True,
    )


def _plan(*steps: McpExecutionStepV1) -> McpExecutionPlanV1:
    ordered = tuple(steps)
    return McpExecutionPlanV1(
        schema_version="mcp-execution-plan-v1",
        plan_id=compute_plan_id(EPISODE, ordered),
        episode_id=EPISODE,
        steps=ordered,
    )


ACTUAL_PREPARE: dict[str, object] = {
    "project_name": "ep-run-1-timeline",
    "timeline_frame_rate": "30",
}
ACTUAL_IMPORT: dict[str, object] = {"source_id": "src-cam-a"}
ACTUAL_PLACE: dict[str, object] = {
    "item_id": "itm-1",
    "source_span": {"start_frame": 0, "end_frame": 90},
    "record_span": {"start_frame": 0, "end_frame": 90},
}
ACTUAL_TRANSFORM: dict[str, object] = {"item_id": "itm-1", "properties": ["zoom_x"]}
ACTUAL_TRANSFORM_WRONG: dict[str, object] = {
    "item_id": "itm-1",
    "properties": ["zoom_x", "crop"],
}
ACTUAL_EQ: dict[str, object] = {"item_ref": "timeline", "state_property": "eq"}


def _happy_plan() -> McpExecutionPlanV1:
    return _plan(_prepare_step(), _import_step(), _place_step())


def _happy_executor() -> FakeExecutor:
    return FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": ACTUAL_PLACE,
        }
    )


def _run(
    runner: McpExecutionRunnerV2, plan: McpExecutionPlanV1, executor: FakeExecutor
) -> McpExecutionRunReportV1:
    return runner.execute(plan, executor=executor, recorder=_recorder())


# ------------------------------------------------- (a) happy plan + ledger


def test_happy_plan_completes_with_ledger_entries(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    report = _run(runner, _happy_plan(), _happy_executor())
    assert report.outcome == "completed"
    assert [s.status for s in report.steps] == ["completed", "completed", "completed"]
    assert report.rung_entries == ()
    assert isinstance(report.calls, McpExecutionReportV1)
    assert report.calls.total_calls == 3
    # misleading_success_output: parse the LEDGER, not in-memory state
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 3
    assert all(record.status == "ok" for record in records)
    assert [record.tool_name for record in records] == [
        "prepare_project",
        "safe_import_media",
        "append_to_timeline",
    ]
    assert [record.readback_refs for record in records] == [
        ("stp-prepare-ep-run-1",),
        ("stp-import-src-cam-a",),
        ("stp-place-itm-1",),
    ]
    store.close()


def test_failed_step_makes_outcome_failed(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor({"prepare_project": ACTUAL_PREPARE, "place_clip": ACTUAL_PLACE})
    report = _run(runner, _happy_plan(), executor)
    assert report.outcome == "failed"
    # Fail-fast contract: the run stops at the first terminal step failure
    # (the unscripted import step), so the report carries the attempted
    # PREFIX ending in the failed step and never dispatches place_clip.
    statuses = {s.step_id: s.status for s in report.steps}
    assert statuses["stp-prepare-ep-run-1"] == "completed"
    assert statuses["stp-import-src-cam-a"] == "failed"
    assert "stp-place-itm-1" not in statuses
    assert not any(action == "place_clip" for _surface, action, _params in executor.calls)


# --------------------------------------- (a2) first terminal failure stops


@pytest.mark.parametrize(
    "failing_actual",
    [
        McpTimeoutError("tools/call", 5.0),
        LiveAdapterError("preset-missing", "0 presets"),
        [dict(ACTUAL_PLACE, item_id="itm-OTHER")],  # readback mismatch, exhausted
    ],
    ids=["timeout", "typed-error", "readback-mismatch"],
)
def test_first_terminal_failure_stops_the_run(
    tmp_path: Path, failing_actual: object
) -> None:
    """The serial runner must stop immediately after the first terminal
    step failure: NO later plan step is dispatched, whatever the failure
    class (exhausted timeout, typed error, or exhausted readback mismatch).

    Live blocker this locks out: the six-hour finishing run kept
    dispatching every later step after the first terminal placement
    timeout, burning the session on a prefix that was already dead."""
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": failing_actual,
            "place_clip": ACTUAL_PLACE,
        }
    )
    report = _run(runner, _happy_plan(), executor)

    assert report.outcome == "failed"
    assert [s.step_id for s in report.steps] == [
        "stp-prepare-ep-run-1",
        "stp-import-src-cam-a",
    ]
    assert report.steps[-1].status == "failed"
    assert not any(action == "place_clip" for _surface, action, _params in executor.calls)
    records = read_call_records(tmp_path / "ledger")
    assert not any(r.action == "place_clip" for r in records)
    store.close()


# ------------------------------------- (b) readback mismatch + retry policy


def test_readback_mismatch_retries_then_succeeds(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": [dict(ACTUAL_PLACE, item_id="itm-OTHER"), ACTUAL_PLACE],
        }
    )
    report = _run(runner, _happy_plan(), executor)
    assert report.outcome == "completed"
    place = next(s for s in report.steps if s.step_id == "stp-place-itm-1")
    assert place.status == "completed"
    # bounded count: exactly two attempts (first mismatched, second matched)
    assert len(place.attempts) == 2
    assert place.attempts[0].status == "ok"
    assert place.attempts[0].readback_matched is False
    assert place.attempts[1].readback_matched is True
    assert "item_id" in place.attempts[0].detail
    records = read_call_records(tmp_path / "ledger")
    place_rows = [r for r in records if r.readback_refs == ("stp-place-itm-1",)]
    assert len(place_rows) == 2
    assert all(r.status == "ok" for r in place_rows)
    store.close()


def test_typed_executor_error_code_survives_into_report(tmp_path: Path) -> None:
    """A typed adapter refusal (LiveAdapterError carries code/detail) must
    surface its code and detail in the run report — not collapse to the bare
    exception class name. Measured on v44-real-01: stp-render-native failed
    as `executor-error | LiveAdapterError` with the real cause invisible."""

    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": LiveAdapterError(
                "render-already-in-progress",
                "render queue busy: job c42fe33f still rendering",
            ),
        }
    )
    report = _run(runner, _happy_plan(), executor)
    place = next(s for s in report.steps if s.step_id == "stp-place-itm-1")
    assert place.status == "failed"
    assert place.failure_code == "executor-error"
    assert "render-already-in-progress" in place.detail
    assert "c42fe33f" in place.detail
    assert "LiveAdapterError" in place.attempts[-1].detail
    assert "render-already-in-progress" in place.attempts[-1].detail
    store.close()


def test_transient_failure_exhausts_bounded_retries(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, retry_policy=RetryPolicy(max_attempts=2))
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": McpTimeoutError("tools/call", 5.0),
        }
    )
    report = _run(runner, _happy_plan(), executor)
    place = next(s for s in report.steps if s.step_id == "stp-place-itm-1")
    assert place.status == "failed"
    assert place.failure_code == "executor-timeout"
    assert len(place.attempts) == 2
    assert [a.status for a in place.attempts] == ["timeout", "timeout"]
    # hung_commands probe: the policy bounds execution — exactly 2 place dispatches
    assert len(executor.calls) == 4  # prepare + import + 2 bounded place attempts
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 4
    store.close()


def test_post_mutation_policy_failure_is_attempted_once(tmp_path: Path) -> None:
    """A typed non-retryable adapter failure — the measured live case:
    ``audio-stage-out-of-range`` raised AFTER the preset mutation succeeded,
    where re-running the step re-measured the already-mutated timeline and
    overwrote attempt-1's real value (-6.198 dB) with 0.0 — must be
    attempted exactly once, preserving the first measurement as evidence."""
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, retry_policy=RetryPolicy(max_attempts=3))
    first = LiveAdapterError(
        "audio-stage-out-of-range",
        "dialogue_cleanup measured noise_reduction -6.198 db outside [3.0, 12.0]",
        retryable=False,
    )
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": first,
        }
    )
    report = _run(runner, _happy_plan(), executor)
    place = next(s for s in report.steps if s.step_id == "stp-place-itm-1")
    assert place.status == "failed"
    assert place.failure_code == "executor-error"
    assert len(place.attempts) == 1
    assert "-6.198" in place.attempts[0].detail
    assert "-6.198" in place.detail
    place_dispatches = [c for c in executor.calls if c[1] == "place_clip"]
    assert len(place_dispatches) == 1
    store.close()


def test_retryable_typed_failure_keeps_bounded_retries(tmp_path: Path) -> None:
    """A typed adapter failure without the non-retryable signal (default)
    keeps the bounded transient retry contract — only an explicit
    ``retryable=False`` opts out."""
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, retry_policy=RetryPolicy(max_attempts=3))
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "import_media": ACTUAL_IMPORT,
            "place_clip": LiveAdapterError(
                "render-already-in-progress", "queue busy (transient)"
            ),
        }
    )
    report = _run(runner, _happy_plan(), executor)
    place = next(s for s in report.steps if s.step_id == "stp-place-itm-1")
    assert place.status == "failed"
    assert len(place.attempts) == 3
    store.close()


# --------------------------------------- (c) permanent failure -> ladder


@pytest.mark.parametrize(
    ("rung", "expected_next"),
    [
        ("mcp_verified_workflow", "mcp_granular_tool"),
        ("external_asset_render", "manual_finalization"),
    ],
)
def test_permanent_failure_records_ladder_transition(
    tmp_path: Path, rung: str, expected_next: str
) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, retry_policy=RetryPolicy(max_attempts=3))
    plan = _plan(_prepare_step(), _transform_step(rung))  # type: ignore[arg-type]
    executor = FakeExecutor(
        {
            "prepare_project": ACTUAL_PREPARE,
            "apply_transform": ACTUAL_TRANSFORM_WRONG,
        }
    )
    report = _run(runner, plan, executor)
    transform = next(s for s in report.steps if s.step_id == "stp-transform-itm-1")
    assert transform.status == "failed"
    expected_attempts = 3 if rung == "mcp_verified_workflow" else 1
    assert len(transform.attempts) == expected_attempts
    entry = next(
        e
        for e in report.rung_entries
        if e.step == "stp-transform-itm-1" and e.source == "runtime_failure"
    )
    assert entry.from_rung == rung
    assert entry.to_rung == expected_next
    assert entry.reason
    store.close()


def test_terminal_rung_failure_has_no_transition(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    plan = _plan(_prepare_step(), _transform_step("unsupported_with_report"))
    executor = FakeExecutor(
        {"prepare_project": ACTUAL_PREPARE, "apply_transform": ACTUAL_TRANSFORM_WRONG}
    )
    report = _run(runner, plan, executor)
    runtime_entries = [e for e in report.rung_entries if e.source == "runtime_failure"]
    assert runtime_entries == []
    transform = next(s for s in report.steps if s.step_id == "stp-transform-itm-1")
    assert transform.status == "failed"
    assert transform.failure_code == "readback-mismatch"
    store.close()


# --------------------------------- (d) fallback steps: explicit rung entry


def test_plan_declared_fallback_step_gets_explicit_entry(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    plan = _plan(_prepare_step(), _eq_fallback_step())
    executor = FakeExecutor({"prepare_project": ACTUAL_PREPARE, "apply_audio_op": ACTUAL_EQ})
    report = _run(runner, plan, executor)
    eq = next(s for s in report.steps if s.step_id == "stp-eq-stage")
    assert eq.status == "completed"
    assert len(report.rung_entries) == 1
    entry = report.rung_entries[0]
    assert entry.source == "plan_declared"
    assert entry.step == "stp-eq-stage"
    assert entry.from_rung == "mcp_verified_workflow"
    assert entry.to_rung == "direct_scripting_gap_adapter"
    assert entry.capability == "audio-property-operation"
    assert entry.status == "failed"
    assert "audio-property-operation" in entry.reason
    store.close()


# ---------------------------------------------- (h) guard integration


def test_backend_flag_refuses_mutating_execution(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, execution_backend="legacy_direct")
    executor = _happy_executor()
    with pytest.raises(BackendPolicyError):
        _run(runner, _happy_plan(), executor)
    assert executor.calls == []
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].tool_name == "prepare_project"
    store.close()


def test_missing_lease_refuses_mutating_execution(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    runner = _make_runner(tmp_path, store)
    executor = _happy_executor()
    with pytest.raises(NotLeaseHolderError):
        _run(runner, _happy_plan(), executor)
    assert executor.calls == []
    store.close()


def test_readonly_step_bypasses_backend_guard(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store, execution_backend="legacy_direct")
    probe = step_from(
        "stp-readonly-probe",
        PrepareProjectParams(
            action="prepare_project", timeline_name="ep-run-1-timeline", fps_num=30, fps_den=1
        ),
        ProjectReadback(kind="project", project_name="ep-run-1-timeline", timeline_frame_rate="30"),
        "prepare_project",
        "mcp_verified_workflow",
        None,
        -2,
        destructive=False,
    )
    report = _run(runner, _plan(probe), FakeExecutor({"prepare_project": ACTUAL_PREPARE}))
    assert report.outcome == "completed"
    store.close()


# ------------------------------------ unmet preconditions fail fast


def test_unmet_precondition_fails_without_dispatch(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor({"place_clip": ACTUAL_PLACE})
    report = _run(runner, _plan(_place_step()), executor)
    place = report.steps[0]
    assert place.status == "failed"
    assert place.failure_code == "precondition"
    assert place.attempts == ()
    assert executor.calls == []
    assert "src-cam-a" in place.detail
    store.close()


# --------------------------------------------- malformed dispatch probe


def test_unknown_action_is_typed_error(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    valid = _plan(_prepare_step())
    smuggled = _prepare_step().model_copy(update={"action": "escape_hatch"})
    poisoned = valid.model_copy(update={"steps": (smuggled,)})  # skips re-validation
    with pytest.raises(UnknownStepActionError) as error:
        _run(runner, poisoned, FakeExecutor({}))
    assert error.value.code == "unknown-action"
    store.close()


# ------------------------------------------------ (g) determinism


def test_report_canonical_bytes_are_deterministic(tmp_path: Path) -> None:
    reports = []
    for suffix in ("one", "two"):
        store = StateStore.open(tmp_path / f"state-{suffix}.sqlite3")
        _acquire_lease(store)
        runner = _make_runner(tmp_path, store, ledger_dir=tmp_path / f"ledger-{suffix}")
        reports.append(_run(runner, _happy_plan(), _happy_executor()))
        store.close()
    assert canonical_model_bytes(reports[0]) == canonical_model_bytes(reports[1])


def test_report_round_trips_through_json(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    report = _run(runner, _happy_plan(), _happy_executor())
    store.close()
    restored = McpExecutionRunReportV1.model_validate(report.model_dump(mode="json"))
    assert restored == report
    assert canonical_model_bytes(restored) == canonical_model_bytes(report)


# ------------------------------------------------ (f) build report rows


def test_build_report_section_shape(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    plan = _plan(_prepare_step(), _eq_fallback_step())
    report = _run(
        runner,
        plan,
        FakeExecutor({"prepare_project": ACTUAL_PREPARE, "apply_audio_op": ACTUAL_EQ}),
    )
    rows = to_build_report_rows(report)
    assert set(rows) == {"warnings", "failures"}
    warning = rows["warnings"][0]
    assert set(warning) == {"code", "detail"}
    assert warning["code"] == "mcp-fallback-direct_scripting_gap_adapter"
    assert "stp-eq-stage" in warning["detail"]
    assert rows["failures"] == ()
    store.close()


def test_failed_step_yields_failure_row(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    executor = FakeExecutor({"prepare_project": ACTUAL_PREPARE, "place_clip": ACTUAL_PLACE})
    report = _run(runner, _happy_plan(), executor)
    rows = to_build_report_rows(report)
    import_failures = [
        row for row in rows["failures"] if "stp-import-src-cam-a" in str(row["code"])
    ]
    assert len(import_failures) == 1
    assert import_failures[0]["code"] == "mcp-step-failed-stp-import-src-cam-a"
    assert "executor-error" in str(import_failures[0]["detail"])
    # fail-fast: the never-attempted place step produces no row at all
    assert not any("stp-place-itm-1" in str(row["code"]) for row in rows["failures"])
    store.close()


# ------------------------------------------------ readback unit checks


def test_verify_readback_missing_key_names_the_key() -> None:
    expected = ImportReadback(kind="import", source_id="src-cam-a")
    result = verify_readback(expected, {"source_id": "src-other"})
    assert result.matched is False
    assert result.mismatches == ("source_id: expected 'src-cam-a', got 'src-other'",)


def test_verify_readback_non_object_actual() -> None:
    result = verify_readback(ImportReadback(kind="import", source_id="src-cam-a"), "imported!")
    assert result.matched is False
    assert result.mismatches == ("actual: not an object",)


def test_verify_readback_placement_span_frames() -> None:
    expected = PlacementReadback(
        kind="placement",
        item_id="itm-1",
        source_span=_SRC_SPAN,
        record_span=_REC_SPAN,
    )
    good = verify_readback(expected, ACTUAL_PLACE)
    assert good.matched is True
    assert good.mismatches == ()
    short = {**ACTUAL_PLACE, "record_span": {"start_frame": 0, "end_frame": 80}}
    bad = verify_readback(expected, short)
    assert bad.matched is False
    assert bad.mismatches == ("record_span: expected (0, 90), got (0, 80)",)


def test_verify_readback_audio_metric_range() -> None:
    expected = AudioMetricReadback(
        kind="audio_metric",
        stage="loudness",
        metric="integrated",
        minimum=-16,
        maximum=-14,
        unit="LUFS",
    )
    ok = verify_readback(
        expected, {"stage": "loudness", "metric": "integrated", "value": -15, "unit": "LUFS"}
    )
    assert ok.matched is True
    over = verify_readback(
        expected, {"stage": "loudness", "metric": "integrated", "value": -10, "unit": "LUFS"}
    )
    assert over.matched is False
    assert any("value" in m for m in over.mismatches)


# --------------------------- subtitle runner-level readback (Task 8 repair)
#
# Measured on v44-real-01: the live subtitle handler returns exact per-cue
# evidence ({"cues": [...]}) while the runner expected CueCountReadback, so
# every attempt failed readback with "cue_count: missing" even though the
# cues existed natively. The runner gate must verify the COMPLETE committed
# cue set — count plus each cue's id, text, and record span.

_HANDLER_CUE_ROWS: tuple[dict[str, object], ...] = (
    {
        "cue_id": "cue-asr-st3",
        "text": "合成ケュー前半、\n合成ケュー後半",
        "record_span": {"start_frame": 0, "end_frame": 96},
        "style": {"font": "Hiragino Sans W3", "size": 0.04, "center": [0.5, 0.14]},
    },
    {
        "cue_id": "cue-asr-st4",
        "text": "合成ケュー最終文",
        "record_span": {"start_frame": 96, "end_frame": 183},
        "style": {"font": "Hiragino Sans W3", "size": 0.04, "center": [0.5, 0.14]},
    },
)


def _expected_cue_readback() -> SubtitleCuesReadback:
    return SubtitleCuesReadback(
        kind="subtitle_cues",
        cues=(
            SubtitleCuePayload(
                cue_id="cue-asr-st3",
                text="合成ケュー前半、\n合成ケュー後半",
                record_span=RecordFrameSpan(start_frame=0, end_frame=96),
            ),
            SubtitleCuePayload(
                cue_id="cue-asr-st4",
                text="合成ケュー最終文",
                record_span=RecordFrameSpan(start_frame=96, end_frame=183),
            ),
        ),
    )


def test_verify_readback_subtitle_cues_matches_handler_evidence() -> None:
    """The exact handler shape (per-cue rows, no cue_count key) verifies
    against the committed cue set — the measured `cue_count: missing`
    failure mode is unrepresentable after the repair."""

    result = verify_readback(_expected_cue_readback(), {"cues": list(_HANDLER_CUE_ROWS)})
    assert result.matched is True
    assert result.mismatches == ()


def test_verify_readback_subtitle_cues_names_drift_per_cue() -> None:
    drifted = [dict(_HANDLER_CUE_ROWS[0]), dict(_HANDLER_CUE_ROWS[1])]
    drifted[0] = {**drifted[0], "text": "別のテキスト"}
    result = verify_readback(_expected_cue_readback(), {"cues": drifted})
    assert result.matched is False
    assert any("cue-asr-st3" in m and "text" in m for m in result.mismatches)

    span_drift = [dict(_HANDLER_CUE_ROWS[0]), dict(_HANDLER_CUE_ROWS[1])]
    span_drift[1] = {
        **span_drift[1],
        "record_span": {"start_frame": 96, "end_frame": 200},
    }
    result_span = verify_readback(_expected_cue_readback(), {"cues": span_drift})
    assert result_span.matched is False
    assert any("cue-asr-st4" in m and "record_span" in m for m in result_span.mismatches)


def test_verify_readback_subtitle_cues_requires_exact_count() -> None:
    short = {"cues": [_HANDLER_CUE_ROWS[0]]}
    result = verify_readback(_expected_cue_readback(), short)
    assert result.matched is False
    assert any("cues" in m and "expected 2" in m for m in result.mismatches)

    missing = verify_readback(_expected_cue_readback(), {})
    assert missing.matched is False
    assert any("cues: missing" in m for m in missing.mismatches)


# ------------------------------------------------ fallback module checks


def test_runtime_transition_enforces_ladder_adjacency() -> None:
    entry = runtime_transition(_transform_step("external_asset_render"), "readback-mismatch")
    assert entry.to_rung == "manual_finalization"
    assert entry.source == "runtime_failure"
    assert entry.from_rung == "external_asset_render"


def test_runtime_transition_terminal_rung_is_typed_error() -> None:
    with pytest.raises(FallbackLadderError) as error:
        runtime_transition(_transform_step("unsupported_with_report"), "readback-mismatch")
    assert error.value.code == "terminal-rung"


def test_rung_entry_model_rejects_non_downward_transition() -> None:
    with pytest.raises(ValidationError, match="rung_not_downward"):
        FallbackRungEntryV1(
            step="stp-x",
            from_rung="manual_finalization",
            to_rung="mcp_verified_workflow",
            reason="upward is not a fallback",
            source="runtime_failure",
        )


def test_plan_declared_entry_none_for_mcp_steps() -> None:
    assert plan_declared_entry(_prepare_step()) is None
    entry = plan_declared_entry(_eq_fallback_step())
    assert entry is not None
    assert entry.to_rung == "direct_scripting_gap_adapter"


# ------------------------------------------------ (e) qc aggregation


def test_qc_summary_from_provider_checks() -> None:
    checks: Sequence[ProviderCheckV1] = (
        ProviderCheckV1(
            check="gaps_overlaps",
            provider="mcp",
            status="pass",
            evidence_ref="mcp://timeline-report#gap-scan",
        ),
        ProviderCheckV1(
            check="loudness",
            provider="advanced",
            status="fail",
            evidence_ref="advanced://loudness-report/run-42",
            detail="integrated -10.0 LUFS > -14",
        ),
        ProviderCheckV1(
            check="delivery_manifest",
            provider="mcp",
            status="not_available",
            evidence_ref="mcp://delivery-manifest",
        ),
    )
    summary = aggregate_technical_qc(checks)
    assert summary.passed == 1
    assert summary.failed == 1
    assert summary.not_available == 1
    assert summary.verdict == "blocked"


def test_checks_from_run_report_reference_step_evidence(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.sqlite3")
    _acquire_lease(store)
    runner = _make_runner(tmp_path, store)
    report = _run(runner, _happy_plan(), _happy_executor())
    store.close()
    checks = checks_from_run_report(report)
    kinds = {c.check for c in checks}
    assert kinds == {"timeline_readback", "conform_source_ranges"}
    for check in checks:
        assert check.provider == "mcp"
        assert check.status == "pass"
        assert check.evidence_ref.startswith("step:")


def test_qc_issue_rows_reference_provider_evidence() -> None:
    summary = aggregate_technical_qc(
        (
            ProviderCheckV1(
                check="loudness",
                provider="advanced",
                status="fail",
                evidence_ref="advanced://loudness-report/run-42",
                detail="integrated -10.0 LUFS",
            ),
        )
    )
    rows = to_qc_issue_rows(summary, input_hashes=("a" * 64,))
    assert len(rows) == 1
    row = rows[0]
    assert row["rule_id"] == "audio_loudness_out_of_range"
    assert row["severity"] == "blocker"
    assert "advanced://loudness-report/run-42" in str(row["detail"])
    evidence = row["evidence"]
    assert isinstance(evidence, dict)
    measured = evidence["measured"]
    assert isinstance(measured, list)
    refs = [m for m in measured if isinstance(m, dict) and m.get("name") == "evidence_ref"]
    assert refs
    assert refs[0].get("value") == "advanced://loudness-report/run-42"
    assert row["input_hashes"] == ["a" * 64]


def test_qc_verdict_degrades_on_not_available_only() -> None:
    summary = aggregate_technical_qc(
        (
            ProviderCheckV1(
                check="missing_media",
                provider="mcp",
                status="not_available",
                evidence_ref="mcp://media-pool",
            ),
        )
    )
    assert summary.verdict == "degraded"
