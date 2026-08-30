"""Single-writer execution runner guard for MCP mutating calls (task 10).

Skeleton for the production runner (PRD v4.3 11.3: the deterministic MCP
Execution Plan runs serially "under the existing single-writer lease").
The full runner lands as ``services/mcp_execution/runner.py`` in task 39;
this module fixes the GUARD CONTRACT around every mutating call:

1. LEASE — the caller must hold the job_runner stage lease. Ownership is
   proven through the store's own public LeaseOps API (``renew_lease``),
   never raw SQL and never a local re-implementation of expiry logic.
2. BACKEND — with ``execution_backend != "mcp"`` (services/config/backends)
   mutating calls are refused by policy; read-only calls pass.
3. MODE — an interactive LLM assist flag exists, but assisted-mode mutating
   calls against a production job are refused. Production detection is
   minimal: ``job_id`` prefixed ``dev-``/``test-`` is non-production; any
   other id is production unless overridden via ``is_production``.

Every refusal appends a task-8 ledger record (``McpExecutionCallV1`` with
``status="error"``) BEFORE raising the typed error, so refusals stay
auditable. Timing uses the job_runner logical-clock convention
(``LogicalClock.now``); no wall clock is read here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, Protocol

from services.config.backends import load_backends
from services.job_runner.stage_runner import STAGE_LEASE_TTL_SECONDS, stage_resource
from services.job_runner.state_errors import StateStoreError
from services.mcp_client.call_models import (
    McpCallRecorder,
    append_call_record,
)
from services.mcp_client.errors import McpClientError

if TYPE_CHECKING:
    from services.job_runner.stage_runner_models import LogicalClock
    from services.job_runner.state_store import StateStore

_DEFAULT_BACKENDS_PATH: Final = Path(__file__).resolve().parents[2] / "config" / "backends.json"
_DEV_TEST_PREFIXES: Final = ("dev-", "test-")


class GuardRefusalError(McpClientError):
    """Base for guard refusals; carries a stable machine-readable code."""

    code: str

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class NotLeaseHolderError(GuardRefusalError):
    """Mutating call attempted without holding the live stage lease."""

    def __init__(self, detail: str = "not-lease-holder") -> None:
        super().__init__("not-lease-holder", detail)


class BackendPolicyError(GuardRefusalError):
    """Mutating call refused because execution_backend != mcp."""

    def __init__(self, detail: str = "backend-policy") -> None:
        super().__init__("backend-policy", detail)


class AssistedModeError(GuardRefusalError):
    """Assisted-mode mutating call against a production job."""

    def __init__(self, detail: str = "assisted-mode-production-refused") -> None:
        super().__init__("assisted-mode-production-refused", detail)


class McpTransportFn(Protocol):
    """Minimal transport seam; the task-7 client satisfies this shape.

    ``timeout_seconds`` is an OPTIONAL per-operation request deadline
    (``None`` = the transport-configured default). Only measured
    long-running operations override it (e.g. the full placement scan);
    implementors forward it to the underlying JSON-RPC request.
    """

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object: ...


def _is_production_job(job_id: str, *, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return not job_id.startswith(_DEV_TEST_PREFIXES)


class McpExecutionRunner:
    """Guarded runner: only this path may mutate under the stage lease."""

    def __init__(
        self,
        *,
        store: StateStore,
        job_id: str,
        stage_name: str,
        holder_token: str,
        ledger_dir: Path,
        clock: LogicalClock,
        transport: McpTransportFn,
        backends_path: Path | str = _DEFAULT_BACKENDS_PATH,
        assisted_mode: bool = False,
        is_production: bool | None = None,
        provider_version: str = "mcp-client-v1",
        resolve_version: str = "21.0.0",
        server_mode: str = "test",
        lease_ttl_seconds: int = STAGE_LEASE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._resource = stage_resource(job_id, stage_name)
        self._holder_token = holder_token
        self._ledger_dir = Path(ledger_dir)
        self._clock = clock
        self._transport = transport
        self._backends_path = backends_path
        self._assisted_mode = assisted_mode
        self._is_production = _is_production_job(job_id, explicit=is_production)
        self._lease_ttl_seconds = lease_ttl_seconds
        # Task-8 recorder owns digests/model construction; the lambda keeps
        # the logical clock live (a bound .now property would freeze).
        self._recorder = McpCallRecorder(
            provider_version=provider_version,
            resolve_version=resolve_version,
            server_mode=server_mode,
            clock=lambda: clock.now,
        )

    def _holds_live_lease(self) -> bool:
        """Prove live ownership through the store's own LeaseOps API.

        ``renew_lease`` is the documented owner-only operation: one atomic
        conditional statement that re-checks holder AND expiry, so success
        proves this exact token holds a live lease at mutation time (and an
        active writer refreshing before mutating is correct single-writer
        behaviour). Any StateStoreError means refusal — fail closed.
        """
        try:
            self._store.renew_lease(
                resource=self._resource,
                holder=self._holder_token,
                now=self._clock.now,
                ttl_seconds=self._lease_ttl_seconds,
            )
        except StateStoreError:
            return False
        return True

    def _audit_and_raise(
        self,
        error: GuardRefusalError,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        request_payload: object,
        started_at: int,
    ) -> NoReturn:
        """Append the refusal to the ledger, then raise the typed error."""
        record = self._recorder.build_record(
            tool_name=tool_name,
            action=action,
            normalized_params=normalized_params,
            request_payload=request_payload,
            response_payload={"error": error.code},
            status="error",
            started_at=started_at,
            finished_at=max(self._clock.now, started_at),
        )
        append_call_record(record, self._ledger_dir)
        raise error

    def _invoke_and_record(
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        request_payload: object,
        started_at: int,
    ) -> object:
        result = self._transport(tool_name, action, normalized_params)
        record = self._recorder.build_record(
            tool_name=tool_name,
            action=action,
            normalized_params=normalized_params,
            request_payload=request_payload,
            response_payload=result,
            status="ok",
            started_at=started_at,
            finished_at=max(self._clock.now, started_at),
        )
        append_call_record(record, self._ledger_dir)
        return result

    def execute(
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object] | None = None,
        request_payload: object = None,
        read_only: bool = False,
    ) -> object:
        """Execute one MCP call subject to the three mutating guards.

        ``read_only=True`` bypasses all guards (observation never mutates).
        Otherwise the guards run in order: LEASE, BACKEND, MODE — each
        refusal is audited before its typed error propagates.
        """
        params: Mapping[str, object] = {} if normalized_params is None else normalized_params
        started_at = self._clock.now
        if read_only:
            return self._invoke_and_record(
                tool_name=tool_name,
                action=action,
                normalized_params=params,
                request_payload=request_payload,
                started_at=started_at,
            )
        if not self._holds_live_lease():
            self._audit_and_raise(
                NotLeaseHolderError(),
                tool_name=tool_name,
                action=action,
                normalized_params=params,
                request_payload=request_payload,
                started_at=started_at,
            )
        if load_backends(self._backends_path).execution_backend != "mcp":
            self._audit_and_raise(
                BackendPolicyError(),
                tool_name=tool_name,
                action=action,
                normalized_params=params,
                request_payload=request_payload,
                started_at=started_at,
            )
        if self._assisted_mode and self._is_production:
            self._audit_and_raise(
                AssistedModeError(),
                tool_name=tool_name,
                action=action,
                normalized_params=params,
                request_payload=request_payload,
                started_at=started_at,
            )
        return self._invoke_and_record(
            tool_name=tool_name,
            action=action,
            normalized_params=params,
            request_payload=request_payload,
            started_at=started_at,
        )


__all__ = [
    "AssistedModeError",
    "BackendPolicyError",
    "GuardRefusalError",
    "McpExecutionRunner",
    "McpTransportFn",
    "NotLeaseHolderError",
]
