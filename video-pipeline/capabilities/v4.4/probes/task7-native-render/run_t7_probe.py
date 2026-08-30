"""Task 7 live probe entrypoint: compile → runner → adapter, twice.

Every summary value is OBSERVED: the two runner-dispatched render_native
results are captured read-only and parsed strictly; same-identity
booleans compare the first and second results. The rerun must make zero
prepare/start vendor calls. The ledger is erased at startup, the
disposable project is closed/deleted BEFORE the final row count, absolute
repo prefixes are scrubbed, and renders/meta/caches/state are removed.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import TYPE_CHECKING

from t7_evidence import (
    cleanup_artifacts,
    count_step_actions,
    count_vendor_actions,
    evidence_verdict,
    ledger_row_count,
    parse_render_result,
    reset_evidence,
    write_summary,
)
from t7_session import (
    BACKENDS,
    STATE_DB,
    build_executor,
    build_plan,
    close_session,
    open_session,
)

if TYPE_CHECKING:
    from services.job_runner.state_store import StateStore
    from services.mcp_client.execution_runner import McpTransportFn
    from services.mcp_execution.plan_models import McpExecutionPlanV1
    from services.mcp_execution.runner import McpExecutionRunReportV1, StepResultV1

SCHEMA = "task7-native-render-v5"
_EXPECTED_RESULTS = 2


def _run_twice(
    executor: object, lineage: dict[str, str]
) -> tuple[McpExecutionRunReportV1, McpExecutionRunReportV1, dict[str, int]]:
    """Two full runner executions; returns both reports plus the vendor
    action counts observed after the FIRST run (the delta baseline)."""

    from t7_session import HOLDER, JOB_ID  # noqa: PLC0415

    from services.config.backends import load_backends, set_backend  # noqa: PLC0415
    from services.job_runner.stage_runner import stage_resource  # noqa: PLC0415
    from services.job_runner.state_store import StateStore  # noqa: PLC0415

    previous_backend = load_backends(BACKENDS).execution_backend
    set_backend("execution_backend", "mcp", path=BACKENDS)
    try:
        store = StateStore.open(STATE_DB)
        try:
            store.acquire_lease(
                resource=stage_resource(JOB_ID, "stage-finish"),
                holder=HOLDER,
                now=1000,
                ttl_seconds=3600,
            )
            plan = build_plan()
            report1 = _execute_once(plan, executor, lineage, store)
            first_counts = count_vendor_actions()
            report2 = _execute_once(plan, executor, lineage, store)
            return report1, report2, first_counts
        finally:
            store.close()
    finally:
        set_backend("execution_backend", previous_backend, path=BACKENDS)


def _execute_once(
    plan: McpExecutionPlanV1,
    executor: object,
    lineage: dict[str, str],
    store: StateStore,
) -> McpExecutionRunReportV1:
    from t7_session import build_recorder, build_runner  # noqa: PLC0415

    runner = build_runner(store)
    recorder = build_recorder(lineage)
    return runner.execute(
        plan,
        executor=executor,  # type: ignore[arg-type] (observing wrapper)
        recorder=recorder,
    )


def _render_step(report: McpExecutionRunReportV1) -> StepResultV1:
    return next(s for s in report.steps if s.step_id == "stp-render-native")


def _require_observed_results(captured: list[object]) -> None:
    if len(captured) != _EXPECTED_RESULTS:
        raise ValueError(
            f"expected {_EXPECTED_RESULTS} observed render results, got {len(captured)}"
        )


def main() -> int:
    reset_evidence()
    started = time.time()
    state = open_session()
    exit_code = 1
    summary: dict[str, object] = {}
    try:
        from t7_session import mod  # noqa: PLC0415

        clip = mod.generate_calibrated_clip()
        captured: list[object] = []
        adapter = build_executor(state, clip)
        executor = instrument_adapter(adapter, captured)
        lineage = {
            "provider_version": state["provider_version"],
            "resolve_version": state["resolve_version"],
        }
        report1, report2, first_counts = _run_twice(executor, lineage)
        _require_observed_results(captured)
        first = parse_render_result(captured[0])
        rerun = parse_render_result(captured[1])
        final_counts = count_vendor_actions()
        rerun_delta = {
            action: final_counts.get(action, 0) - first_counts.get(action, 0)
            for action in sorted(set(final_counts) | set(first_counts))
        }
        step1, step2 = _render_step(report1), _render_step(report2)
        summary = {
            "schema": SCHEMA,
            "route": (
                "production emitters -> McpExecutionPlanV1 -> McpExecutionRunnerV2 "
                "-> LiveMcpAdapter -> pinned MCP"
            ),
            "plan_id": report1.plan_id,
            "execution_report_first_outcome": report1.outcome,
            "execution_report_rerun_outcome": report2.outcome,
            "render_step_status": [step1.status, step2.status],
            "first": first,
            "rerun": {
                **rerun,
                "real_job_id": rerun["job_id"] != "reused-existing",
                "same_job_id_as_first": rerun["job_id"] == first["job_id"],
                "same_output_as_first": rerun["output_path"] == first["output_path"],
                "same_sha256_as_first": rerun["output_sha256"] == first["output_sha256"],
                "rerun_vendor_call_delta": rerun_delta,
                "zero_new_prepare": rerun_delta.get("prepare_render_job", 0) == 0,
                "zero_new_start": rerun_delta.get("start", 0) == 0,
                "step_action_counts_final": count_step_actions(),
            },
            "wall_seconds": round(time.time() - started, 3),
        }
        verdict = evidence_verdict(
            first,
            rerun,
            first_counts,
            rerun_delta,
            [step1.status, step2.status],
        )
        summary["verdict"] = verdict
        exit_code = 0 if verdict["passed"] is True else 1
    except Exception as exc:  # noqa: BLE001
        summary = {
            "schema": SCHEMA,
            "error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": round(time.time() - started, 3),
        }
    finally:
        with contextlib.suppress(Exception):
            close_session(state)
        summary["ledger_rows_final"] = ledger_row_count()
        write_summary(summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        cleanup_artifacts()
    return exit_code


def instrument_adapter(
    adapter: McpTransportFn, captured: list[object]
) -> McpTransportFn:
    """Read-only observation of each dispatched render_native result."""

    def observed(
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        result = adapter(
            tool_name, action, normalized_params, timeout_seconds=timeout_seconds
        )
        if action == "render_native":
            captured.append(result)
        return result

    return observed


if __name__ == "__main__":
    raise SystemExit(main())
