"""Serial plan-execution runner with readback + fallback ladder (task 39).

# allow: SIZE_OK — task 39 pins the commit scope to these four mcp_execution
# modules; the runner owns one serial execution loop (guards -> dispatch ->
# record -> verify -> retry/fallback -> report) whose stages share state.

:class:`McpExecutionRunnerV2` executes a committed
:class:`~services.mcp_execution.plan_models.McpExecutionPlanV1` step by
step, SERIALLY, under the task-10 single-writer guard semantics — before
every MUTATING dispatch the runner proves lease ownership through the
store's public LeaseOps (``renew_lease``), refuses when the backend flag
is not ``mcp``, and refuses assisted-mode mutation of production jobs.
Every refusal is ledger-audited first (task-8 rows), exactly like
``services.mcp_client.execution_runner``. Read-only steps
(``destructive=False``) bypass the guards.

Per step: typed-action dispatch (table-driven; an unknown action is a
typed refusal, never a guess), one ledger row PER ATTEMPT via the
caller-supplied task-8 ``McpCallRecorder``, structural readback
verification (:mod:`services.mcp_execution.readback` — mismatch = step
failure), bounded retries for ``transient`` steps (injectable
:class:`RetryPolicy`; ``permanent`` steps get one attempt), and on
terminal failure the PRD §19 ladder transition is recorded
(:mod:`services.mcp_execution.fallback`) — silent downgrades are
structurally impossible: every rung change appears in the report.

The report here is the RUN-level artifact (schema
``mcp-execution-run-report-v1``): T8's ``McpExecutionReportV1``
(``mcp-execution-report-v1``) remains the per-call ledger aggregation and
is embedded verbatim as ``calls`` — two contracts, two schema strings.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, NoReturn

from pydantic import BeforeValidator, Field

from services.config.backends import load_backends
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.job_runner.stage_runner import STAGE_LEASE_TTL_SECONDS, stage_resource
from services.job_runner.state_errors import StateStoreError
from services.mcp_client.call_models import (
    McpExecutionReportV1,
    StatusLiteral,
    aggregate_calls,
    append_call_record,
)
from services.mcp_client.errors import McpTimeoutError
from services.mcp_client.execution_runner import (
    AssistedModeError,
    BackendPolicyError,
    McpTransportFn,
    NotLeaseHolderError,
)
from services.mcp_execution.fallback import (
    FallbackRungEntryV1,
    plan_declared_entry,
    runtime_transition,
)
from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.plan_models import (  # noqa: TC001 (pydantic field types)
    FallbackRung,
    McpExecutionPlanV1,
)
from services.mcp_execution.plan_payloads import (
    ImportMediaParams,
    PlaceAudioParams,
    PlaceClipParams,
    PlaceOverlayParams,
    PlaceTitleParams,
)
from services.mcp_execution.readback import verify_readback

if TYPE_CHECKING:
    from services.job_runner.stage_runner_models import LogicalClock
    from services.job_runner.state_store import StateStore
    from services.mcp_client.call_models import McpCallRecorder, McpExecutionCallV1
    from services.mcp_execution.plan_models import McpExecutionStepV1

_DEV_TEST_PREFIXES: Final = ("dev-", "test-")
_DEFAULT_BACKENDS_PATH: Final = Path(__file__).resolve().parents[2] / "config" / "backends.json"


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


# Action -> execution kind (the dispatch table): an action absent from it
# cannot be executed (typed refusal); the kind fixes the run-state a
# completed step contributes. bootstrap -> project+timeline ready,
# import -> media_imported, place -> items_placed, effect -> none.
_ACTION_KIND: Final[dict[str, str]] = {
    **dict.fromkeys(("prepare_project",), "bootstrap"),
    **dict.fromkeys(("import_media",), "import"),
    **dict.fromkeys(("place_clip", "place_overlay", "place_title", "place_audio"), "place"),
    **dict.fromkeys(
        (
            "apply_subtitles",
            "apply_voice_isolation",
            "apply_audio_op",
            "apply_audio_stage",
            "apply_ducking",
            "apply_color",
            "apply_transform",
            "apply_speed_change",
            "apply_transition",
            "manual_required",
            "apply_kit_recipe",
            "render_native",
        ),
        "effect",
    ),
}


class UnknownStepActionError(ValueError):
    """Dispatch-table refusal for an action outside the closed vocabulary."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RetryPolicy(StrictModel):
    """Bounded retry budget for ``transient`` steps (injectable)."""

    max_attempts: int = Field(default=3, ge=1, strict=True)


class StepAttemptV1(StrictModel):
    """One dispatched attempt (one ledger row per attempt)."""

    attempt: int = Field(ge=1, strict=True)
    status: StatusLiteral
    readback_matched: bool | None = None
    detail: str = ""


class StepResultV1(StrictModel):
    """Terminal outcome of one plan step."""

    step_id: Identifier
    action: str = Field(min_length=1, strict=True)
    rung: FallbackRung
    status: Literal["completed", "failed"]
    attempts: Annotated[tuple[StepAttemptV1, ...], BeforeValidator(_to_tuple)] = ()
    failure_code: str | None = None
    detail: str = ""


class McpExecutionRunReportV1(StrictModel):
    """Run-level MCP execution report (embeds the T8 call aggregation)."""

    schema_version: Literal["mcp-execution-run-report-v1"]
    plan_id: Sha256
    episode_id: Identifier
    outcome: Literal["completed", "failed"]
    steps: Annotated[tuple[StepResultV1, ...], BeforeValidator(_to_tuple)] = Field(min_length=1)
    rung_entries: Annotated[tuple[FallbackRungEntryV1, ...], BeforeValidator(_to_tuple)] = ()
    calls: McpExecutionReportV1
    started_at: int = Field(ge=0, strict=True)
    finished_at: int = Field(ge=0, strict=True)


class _RunState:
    """Mutable per-run precondition state (never part of the artifact)."""

    def __init__(self) -> None:
        self.project_ready = False
        self.timeline_ready = False
        self.media_imported: set[str] = set()
        self.items_placed: set[str] = set()


def _unmet_preconditions(step: McpExecutionStepV1, state: _RunState) -> str | None:
    pre = step.preconditions
    problems: list[str] = []
    if pre.project_ready and not state.project_ready:
        problems.append("project_ready")
    if pre.timeline_ready and not state.timeline_ready:
        problems.append("timeline_ready")
    missing_media = sorted(s for s in pre.media_imported if s not in state.media_imported)
    if missing_media:
        problems.append(f"media_imported:{missing_media}")
    missing_items = sorted(i for i in pre.items_placed if i not in state.items_placed)
    if missing_items:
        problems.append(f"items_placed:{missing_items}")
    return "; ".join(problems) if problems else None


def _apply_state(step: McpExecutionStepV1, state: _RunState) -> None:
    kind = _ACTION_KIND[step.action]
    params = step.normalized_params
    if kind == "bootstrap":
        state.project_ready = True
        state.timeline_ready = True
    elif kind == "import" and isinstance(params, ImportMediaParams):
        state.media_imported.add(params.source_id)
    elif kind == "place" and isinstance(
        params, PlaceClipParams | PlaceOverlayParams | PlaceTitleParams | PlaceAudioParams
    ):
        state.items_placed.add(params.item_id)


class McpExecutionRunnerV2:
    """Serial single-writer plan runner (guards + readback + fallback)."""

    def __init__(  # noqa: PLR0913 (guard knobs mirror the task-10 runner)
        self,
        *,
        store: StateStore,
        job_id: str,
        stage_name: str,
        holder_token: str,
        ledger_dir: Path,
        clock: LogicalClock,
        backends_path: Path | str = _DEFAULT_BACKENDS_PATH,
        assisted_mode: bool = False,
        is_production: bool | None = None,
        retry_policy: RetryPolicy | None = None,
        lease_ttl_seconds: int = STAGE_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._resource = stage_resource(job_id, stage_name)
        self._holder_token = holder_token
        self._ledger_dir = Path(ledger_dir)
        self._clock = clock
        self._backends_path = backends_path
        self._assisted_mode = assisted_mode
        self._is_production = (
            not job_id.startswith(_DEV_TEST_PREFIXES) if is_production is None else is_production
        )
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._lease_ttl_seconds = lease_ttl_seconds

    # ------------------------------------------------- guards (T10 order)

    def _audit_and_raise(
        self,
        recorder: McpCallRecorder,
        error: NotLeaseHolderError | BackendPolicyError | AssistedModeError,
        step: McpExecutionStepV1,
        payload: dict[str, object],
        started_at: int,
    ) -> NoReturn:
        record = recorder.build_record(
            tool_name=step.tool_surface,
            action=step.action,
            normalized_params=payload,
            request_payload=payload,
            response_payload={"error": error.code},
            status="error",
            readback_refs=(step.step_id,),
            started_at=started_at,
            finished_at=max(self._clock.now, started_at),
        )
        append_call_record(record, self._ledger_dir)
        raise error

    def _guard_mutating_step(
        self,
        recorder: McpCallRecorder,
        step: McpExecutionStepV1,
        payload: dict[str, object],
    ) -> None:
        """LEASE -> BACKEND -> MODE; read-only steps bypass all three."""
        if not step.destructive:
            return
        started = self._clock.now
        try:
            self._store.renew_lease(
                resource=self._resource,
                holder=self._holder_token,
                now=self._clock.now,
                ttl_seconds=self._lease_ttl_seconds,
            )
        except StateStoreError:
            self._audit_and_raise(recorder, NotLeaseHolderError(), step, payload, started)
        if load_backends(self._backends_path).execution_backend != "mcp":
            self._audit_and_raise(recorder, BackendPolicyError(), step, payload, started)
        if self._assisted_mode and self._is_production:
            self._audit_and_raise(recorder, AssistedModeError(), step, payload, started)

    # ---------------------------------------------------- one attempt

    @staticmethod
    def _error_identity(exc: Exception) -> str:
        """Typed ``code``/``detail`` when the executor's exception carries
        them (the LiveAdapterError / StateStoreError envelope convention),
        else the bare class name. The run report's attempt detail must name
        the typed cause — ``LiveAdapterError`` alone hid the real blocker
        on the v44-real-01 run."""

        code = getattr(exc, "code", None)
        detail = getattr(exc, "detail", None)
        name = type(exc).__name__
        if not isinstance(code, str):
            return name
        if isinstance(detail, str) and detail:
            return f"{name}({code}): {detail}"
        return f"{name}({code})"

    def _attempt(
        self,
        step: McpExecutionStepV1,
        executor: McpTransportFn,
        recorder: McpCallRecorder,
        index: int,
        records: list[McpExecutionCallV1],
    ) -> tuple[StepAttemptV1, bool]:
        """One attempt plus its retryability: ``False`` means the failure
        is a typed deterministic verdict (e.g. a measured-policy gate on an
        already-mutated timeline) whose retry could only re-observe the
        mutated state — the loop must not spend the retry budget on it."""
        payload = step.normalized_params.model_dump(mode="json")
        self._guard_mutating_step(recorder, step, payload)
        started = self._clock.now
        status: StatusLiteral = "ok"
        error_name = ""
        retryable = True
        actual: object = None
        try:
            actual = executor(step.tool_surface, step.action, payload)
        except McpTimeoutError as exc:
            status = "timeout"
            error_name = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 (attempt outcome mapping for the ledger)
            status = "error"
            error_name = self._error_identity(exc)
            retryable = not (isinstance(exc, LiveAdapterError) and not exc.retryable)
        verification = verify_readback(step.expected_readback, actual) if status == "ok" else None
        record = recorder.build_record(
            tool_name=step.tool_surface,
            action=step.action,
            normalized_params=payload,
            request_payload=payload,
            response_payload=actual if status == "ok" else {"error": error_name},
            status=status,
            readback_refs=(step.step_id,),
            started_at=started,
            finished_at=max(self._clock.now, started),
        )
        append_call_record(record, self._ledger_dir)
        records.append(record)
        if verification is None:
            return (
                StepAttemptV1(
                    attempt=index, status=status, readback_matched=None, detail=error_name or status
                ),
                retryable,
            )
        return (
            StepAttemptV1(
                attempt=index,
                status="ok",
                readback_matched=verification.matched,
                detail="ok" if verification.matched else "; ".join(verification.mismatches),
            ),
            True,
        )

    # -------------------------------------------------------- the run

    def execute(
        self,
        plan: McpExecutionPlanV1,
        *,
        executor: McpTransportFn,
        recorder: McpCallRecorder,
    ) -> McpExecutionRunReportV1:
        """Execute the plan serially; return the run report."""
        started = self._clock.now
        state = _RunState()
        records: list[McpExecutionCallV1] = []
        rung_entries: list[FallbackRungEntryV1] = [
            entry
            for entry in (plan_declared_entry(step) for step in plan.steps)
            if entry is not None
        ]
        results: list[StepResultV1] = []
        for step in plan.steps:
            if step.action not in _ACTION_KIND:
                raise UnknownStepActionError(
                    "unknown-action",
                    f"step {step.step_id} action {step.action!r} is outside the dispatch table",
                )
            unmet = _unmet_preconditions(step, state)
            if unmet is not None:
                results.append(
                    StepResultV1(
                        step_id=step.step_id,
                        action=step.action,
                        rung=step.rung,
                        status="failed",
                        failure_code="precondition",
                        detail=f"unmet preconditions: {unmet}",
                    )
                )
                break
            max_attempts = self._retry_policy.max_attempts if step.retry_class == "transient" else 1
            attempts: list[StepAttemptV1] = []
            failure_code: str | None = None
            for index in range(1, max_attempts + 1):
                attempt, retryable = self._attempt(step, executor, recorder, index, records)
                attempts.append(attempt)
                if attempt.status != "ok":
                    failure_code = f"executor-{attempt.status}"
                    if not retryable:
                        break
                elif attempt.readback_matched is False:
                    failure_code = "readback-mismatch"
                else:
                    failure_code = None
                    break
            if failure_code is None:
                _apply_state(step, state)
                results.append(
                    StepResultV1(
                        step_id=step.step_id,
                        action=step.action,
                        rung=step.rung,
                        status="completed",
                        attempts=tuple(attempts),
                    )
                )
                continue
            if step.fallback != step.rung:
                rung_entries.append(runtime_transition(step, failure_code))
            results.append(
                StepResultV1(
                    step_id=step.step_id,
                    action=step.action,
                    rung=step.rung,
                    status="failed",
                    attempts=tuple(attempts),
                    failure_code=failure_code,
                    detail=attempts[-1].detail if attempts else "",
                )
            )
            # Fail-fast: the serial single-writer run stops at the FIRST
            # terminal step failure — later steps are never dispatched and
            # the report truthfully carries the attempted prefix ending in
            # the failed step (live blocker: the six-hour finishing run
            # kept dispatching ~200 later steps after the first terminal
            # placement timeout).
            break
        outcome = "completed" if all(r.status == "completed" for r in results) else "failed"
        return McpExecutionRunReportV1(
            schema_version="mcp-execution-run-report-v1",
            plan_id=plan.plan_id,
            episode_id=plan.episode_id,
            outcome=outcome,
            steps=tuple(results),
            rung_entries=tuple(rung_entries),
            calls=aggregate_calls(tuple(records)),
            started_at=started,
            finished_at=self._clock.now,
        )


__all__ = [
    "McpExecutionRunReportV1",
    "McpExecutionRunnerV2",
    "RetryPolicy",
    "StepAttemptV1",
    "StepResultV1",
    "UnknownStepActionError",
]
