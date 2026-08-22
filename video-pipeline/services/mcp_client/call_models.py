"""Execution-call lineage artifact for every MCP call (task 8).

Each :class:`McpExecutionCallV1` is an immutable, append-only ledger row:
no response bodies are stored — only sha256 digests (no PII/credentials).
The ledger is a plain JSONL file written with the same append-and-fsync
semantics as the audit service, and aggregated into
:class:`McpExecutionReportV1` via a pure function.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.mcp_client.errors import McpTimeoutError

StatusLiteral = Literal["ok", "error", "timeout"]


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


class McpExecutionCallV1(StrictModel):
    """Immutable lineage row for a single MCP tool call."""

    provider_version: str = Field(min_length=1)
    resolve_version: str = Field(min_length=1)
    server_mode: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    action: str = Field(min_length=1)
    normalized_params_sha256: Sha256
    request_sha256: Sha256
    response_sha256: Sha256
    started_at: int = Field(ge=0, strict=True)
    finished_at: int = Field(ge=0, strict=True)
    status: StatusLiteral
    readback_refs: tuple[str, ...] = Field(default_factory=tuple)


class McpExecutionReportV1(StrictModel):
    """Aggregation of execution-call records (mcp-execution-report-v1)."""

    schema_version: Literal["mcp-execution-report-v1"] = "mcp-execution-report-v1"
    total_calls: int = Field(ge=0, strict=True)
    by_status: dict[str, int]
    by_tool: dict[str, int]
    window_started_at: int | None = Field(default=None, ge=0, strict=False)
    window_finished_at: int | None = Field(default=None, ge=0, strict=False)


def aggregate_calls(records: Sequence[McpExecutionCallV1]) -> McpExecutionReportV1:
    """Pure aggregation over execution-call records."""
    total = len(records)
    by_status: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    window_started: int | None = None
    window_finished: int | None = None
    for record in records:
        by_status[record.status] = by_status.get(record.status, 0) + 1
        by_tool[record.tool_name] = by_tool.get(record.tool_name, 0) + 1
        if window_started is None or record.started_at < window_started:
            window_started = record.started_at
        if window_finished is None or record.finished_at > window_finished:
            window_finished = record.finished_at
    return McpExecutionReportV1(
        total_calls=total,
        by_status=by_status,
        by_tool=by_tool,
        window_started_at=window_started,
        window_finished_at=window_finished,
    )


LEDGER_FILENAME = "mcp-call-ledger.jsonl"


def append_call_record(record: McpExecutionCallV1, ledger_dir: Path) -> Path:
    """Append one canonical JSON line; prior lines stay byte-identical."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / LEDGER_FILENAME
    line = canonical_model_bytes(record) + b"\n"
    with path.open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def read_call_records(ledger_dir: Path) -> tuple[McpExecutionCallV1, ...]:
    """Read all records from the ledger directory (empty tuple if absent)."""
    path = ledger_dir / LEDGER_FILENAME
    if not path.exists():
        return ()
    records: list[McpExecutionCallV1] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        records.append(McpExecutionCallV1.model_validate_json(line))
    return tuple(records)


class McpCallRecorder:
    """Wraps a call, timestamps before/after, digests three payloads.

    The recorder never stores bodies — only sha256 digests.  It is
    composable via constructor injection and does not modify the task-7
    client/transport modules.
    """

    def __init__(
        self,
        *,
        provider_version: str,
        resolve_version: str,
        server_mode: str,
        clock: Callable[[], int] | object | None = None,
    ) -> None:
        self._provider_version = provider_version
        self._resolve_version = resolve_version
        self._server_mode = server_mode
        self._clock = _resolve_clock(clock)

    def build_record(  # noqa: PLR0913
        self,
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object] | object,
        request_payload: object,
        response_payload: object,
        status: StatusLiteral,
        readback_refs: tuple[str, ...] = (),
        started_at: int | None = None,
        finished_at: int | None = None,
    ) -> McpExecutionCallV1:
        start = self._clock() if started_at is None else started_at
        end = self._clock() if finished_at is None else finished_at
        end = max(end, start)
        return McpExecutionCallV1(
            provider_version=self._provider_version,
            resolve_version=self._resolve_version,
            server_mode=self._server_mode,
            tool_name=tool_name,
            action=action,
            normalized_params_sha256=_canonical_sha(normalized_params),
            request_sha256=_digest_payload(request_payload),
            response_sha256=_digest_payload(response_payload),
            started_at=start,
            finished_at=end,
            status=status,
            readback_refs=tuple(readback_refs),
        )

    def wrap_call(  # noqa: PLR0913
        self,
        func: Callable[[], Any],
        *,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object] | object,
        request_payload: object,
        readback_refs: tuple[str, ...] = (),
    ) -> tuple[Any, McpExecutionCallV1]:
        """Execute *func*, time it, digest payloads, map exceptions to status."""
        started_at = self._clock()
        status: StatusLiteral = "ok"
        result: Any = None
        raised: BaseException | None = None
        try:
            result = func()
        except McpTimeoutError as exc:
            status = "timeout"
            result = {"error": str(exc)}
            raised = exc
        except Exception as exc:  # noqa: BLE001  (map any failure to error status for the ledger)
            status = "error"
            result = {"error": str(exc)}
            raised = exc
        finished_at = self._clock()
        finished_at = max(finished_at, started_at)
        record = McpExecutionCallV1(
            provider_version=self._provider_version,
            resolve_version=self._resolve_version,
            server_mode=self._server_mode,
            tool_name=tool_name,
            action=action,
            normalized_params_sha256=_canonical_sha(normalized_params),
            request_sha256=_digest_payload(request_payload),
            response_sha256=_digest_payload(result),
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            readback_refs=tuple(readback_refs),
        )
        if raised is not None:
            raise raised  # type: ignore[misc]
        return result, record


__all__ = [
    "LEDGER_FILENAME",
    "McpCallRecorder",
    "McpExecutionCallV1",
    "McpExecutionReportV1",
    "aggregate_calls",
    "append_call_record",
    "read_call_records",
]
