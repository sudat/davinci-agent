"""Single-writer execution runner guard for MCP mutating calls (task 10).

Enforces three guards around any mutating MCP call while remaining
audit-able via the task-8 ledger. The runner reuses the existing
``services.job_runner`` lease (stage_resource / StateStore LeaseOps)
and the ``services.config.backends`` flag — it does NOT re-implement
leases. Read-only calls (``read_only=True``) bypass all mutating
guards. Every guard refusal appends a ``McpExecutionCallV1`` with
``status="error"`` before raising the typed error.

Production detection is minimal: ``job_id`` starting with ``dev-`` or
``test-`` is considered non-production (dev/test marker); any other
prefix is production. An explicit ``is_production`` override is also
supported for callers that carry job-status explicitly.
"""

from __future__ import annotations  # noqa: I001

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from services.config.backends import load_backends
from services.job_runner.stage_runner import stage_resource
from services.mcp_client.call_models import McpExecutionCallV1, append_call_record
from services.mcp_client.errors import McpClientError


_DEFAULT_BACKENDS_PATH = Path(__file__).resolve().parents[2] / "config" / "backends.json"


class NotLeaseHolderError(McpClientError):
    """Mutating call attempted without holding the stage lease."""

    def __init__(self, detail: str = "not-lease-holder") -> None:
        super().__init__(detail)
        self.code = "not-lease-holder"


class BackendPolicyError(McpClientError):
    """Mutating call refused because execution_backend != mcp."""

    def __init__(self, detail: str = "backend-policy") -> None:
        super().__init__(detail)
        self.code = "backend-policy"


class AssistedModeError(McpClientError):
    """Assisted-mode mutating call against a production job."""

    def __init__(self, detail: str = "assisted-mode-production-refused") -> None:
        super().__init__(detail)
        self.code = "assisted-mode-production-refused"


def _canonical_sha(value: object) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest_payload(payload: object) -> str:
    if isinstance(payload, bytes):
        return _sha_bytes(payload)
    if isinstance(payload, str):
        return hashlib.sha256(payload.encode()).hexdigest()
    return _canonical_sha(payload)


def _resolve_clock(
    clock: Callable[[], int] | object | None,
) -> Callable[[], int]:
    if clock is None:
        return lambda: int(time.time())
    if callable(clock):
        return clock  # type: ignore[return-value]
    now_attr = getattr(clock, "now", None)
    if now_attr is not None:
        if callable(now_attr):
            return lambda: int(now_attr())  # type: ignore[no-redef]
        return lambda: int(now_attr)  # type: ignore[no-redef]
    raise TypeError("clock must be Callable[[], int] or have .now")


def _is_production_job(job_id: str, *, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return not job_id.startswith(("dev-", "test-"))


def _holds_lease(
    store: Any, job_id: str, stage_name: str, holder_token: str, now: int
) -> bool:
    resource = stage_resource(job_id, stage_name)
    row = store._connection.execute(
        "SELECT holder, expires_at FROM leases WHERE resource = ?",
        (resource,),
    ).fetchone()
    if row is None:
        return False
    current_holder = str(row[0])
    expires_at = int(row[1])
    return current_holder == holder_token and expires_at > now


class McpExecutionRunner:
    """Guarded runner for MCP calls under the single-writer stage lease."""

    def __init__(
        self,
        *,
        store: Any,
        job_id: str,
        stage_name: str,
        holder_token: str,
        ledger_dir: Path,
        backends_path: Path | str = _DEFAULT_BACKENDS_PATH,
        clock: Callable[[], int] | object | None = None,
        assisted_mode: bool = False,
        is_production: bool | None = None,
        provider_version: str = "mcp-client-v1",
        resolve_version: str = "21.0.0",
        server_mode: str = "test",
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._job_id = job_id
        self._stage_name = stage_name
        self._holder_token = holder_token
        self._ledger_dir = Path(ledger_dir)
        self._backends_path = Path(backends_path)
        self._clock = _resolve_clock(clock)
        self._assisted_mode = assisted_mode
        self._is_production = _is_production_job(job_id, explicit=is_production)
        self._provider_version = provider_version
        self._resolve_version = resolve_version
        self._server_mode = server_mode
        self._transport = transport

    def _build_record(
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: object,
        request_payload: object,
        response_payload: object,
        status: str,
        started_at: int,
        finished_at: int,
    ) -> McpExecutionCallV1:
        return McpExecutionCallV1(
            provider_version=self._provider_version,
            resolve_version=self._resolve_version,
            server_mode=self._server_mode,
            tool_name=tool_name,
            action=action,
            normalized_params_sha256=_canonical_sha(normalized_params),
            request_sha256=_digest_payload(request_payload),
            response_sha256=_digest_payload(response_payload),
            started_at=started_at,
            finished_at=max(finished_at, started_at),
            status=status,  # type: ignore[arg-type]
            readback_refs=(),
        )

    def _invoke_transport(
        self, tool_name: str, action: str, normalized_params: object
    ) -> Any:
        if self._transport is None:
            return {"ok": True, "tool": tool_name, "action": action}
        try:
            return self._transport(tool_name, action, normalized_params)  # type: ignore[call-arg]
        except TypeError:
            try:
                return self._transport()  # type: ignore[call-arg]
            except TypeError as exc:
                raise TypeError(f"transport call failed: {exc}") from exc

    def execute(
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object] | object = None,
        request_payload: object = None,
        read_only: bool = False,
    ) -> Any:
        """Execute one MCP call subject to the three mutating guards."""
        if normalized_params is None:
            normalized_params = {}
        if request_payload is None:
            request_payload = normalized_params
        started_at = self._clock()
        # Read-only bypasses all mutating guards.
        if read_only:
            result = self._invoke_transport(tool_name, action, normalized_params)
            finished_at = self._clock()
            record = self._build_record(
                tool_name=tool_name,
                action=action,
                normalized_params=normalized_params,
                request_payload=request_payload,
                response_payload=result,
                status="ok",
                started_at=started_at,
                finished_at=finished_at,
            )
            append_call_record(record, self._ledger_dir)
            return result

        # 1. LEASE guard
        now = started_at
        if not _holds_lease(
            self._store, self._job_id, self._stage_name, self._holder_token, now
        ):
            finished_at = self._clock()
            err_payload = {"error": "not-lease-holder"}
            record = self._build_record(
                tool_name=tool_name,
                action=action,
                normalized_params=normalized_params,
                request_payload=request_payload,
                response_payload=err_payload,
                status="error",
                started_at=started_at,
                finished_at=finished_at,
            )
            append_call_record(record, self._ledger_dir)
            raise NotLeaseHolderError("not-lease-holder")

        # 2. BACKEND FLAG guard
        backends = load_backends(self._backends_path)
        if backends.execution_backend != "mcp":
            finished_at = self._clock()
            err_payload = {"error": "backend-policy"}
            record = self._build_record(
                tool_name=tool_name,
                action=action,
                normalized_params=normalized_params,
                request_payload=request_payload,
                response_payload=err_payload,
                status="error",
                started_at=started_at,
                finished_at=finished_at,
            )
            append_call_record(record, self._ledger_dir)
            raise BackendPolicyError("backend-policy")

        # 3. MODE guard
        if self._assisted_mode and self._is_production:
            finished_at = self._clock()
            err_payload = {"error": "assisted-mode-production-refused"}
            record = self._build_record(
                tool_name=tool_name,
                action=action,
                normalized_params=normalized_params,
                request_payload=request_payload,
                response_payload=err_payload,
                status="error",
                started_at=started_at,
                finished_at=finished_at,
            )
            append_call_record(record, self._ledger_dir)
            raise AssistedModeError("assisted-mode-production-refused")

        result = self._invoke_transport(tool_name, action, normalized_params)
        finished_at = self._clock()
        record = self._build_record(
            tool_name=tool_name,
            action=action,
            normalized_params=normalized_params,
            request_payload=request_payload,
            response_payload=result,
            status="ok",
            started_at=started_at,
            finished_at=finished_at,
        )
        append_call_record(record, self._ledger_dir)
        return result

    # Alias for callers that prefer ``call`` naming.
    def call(
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object] | object = None,
        request_payload: object = None,
        read_only: bool = False,
    ) -> Any:
        return self.execute(
            tool_name=tool_name,
            action=action,
            normalized_params=normalized_params,
            request_payload=request_payload,
            read_only=read_only,
        )


__all__ = [
    "AssistedModeError",
    "BackendPolicyError",
    "McpExecutionRunner",
    "NotLeaseHolderError",
]
