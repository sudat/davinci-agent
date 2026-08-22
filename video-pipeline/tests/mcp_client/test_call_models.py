"""MCP execution-call lineage artifact — task 8 TDD."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.mcp_client.call_models import (
    McpCallRecorder,
    McpExecutionCallV1,
    aggregate_calls,
    append_call_record,
    read_call_records,
)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _payload_sha(payload: object) -> str:
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest()
    if isinstance(payload, str):
        return hashlib.sha256(payload.encode()).hexdigest()
    return _canonical_sha(payload)


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "provider_version": "2.98.3",
        "resolve_version": "21.0.4.5",
        "server_mode": "compound",
        "tool_name": "resolve_control",
        "action": "get_version",
        "normalized_params_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "response_sha256": "c" * 64,
        "started_at": 100,
        "finished_at": 200,
        "status": "ok",
        "readback_refs": (),
    }
    base.update(overrides)
    return base


# (a) record construction + full round-trip
def test_record_construction_and_round_trip() -> None:
    record = McpExecutionCallV1(**_valid_kwargs())  # type: ignore[arg-type]
    assert record.provider_version == "2.98.3"
    assert record.readback_refs == ()
    # frozen immutability (append-only ledger semantics: 追記不能)
    with pytest.raises(ValidationError):
        record.provider_version = "mutated"  # type: ignore[misc]
    encoded = record.model_validate_json(record.model_dump_json())
    assert encoded == record
    # canonical bytes round-trip
    line = canonical_model_bytes(record)
    assert McpExecutionCallV1.model_validate_json(line) == record


# (b) bad sha (short hex) → ValidationError (malformed_input probe)
@pytest.mark.parametrize("field", ["normalized_params_sha256", "request_sha256", "response_sha256"])
def test_bad_sha_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        McpExecutionCallV1(**_valid_kwargs(**{field: "abc"}))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        McpExecutionCallV1(**_valid_kwargs(**{field: "z" * 64}))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        McpExecutionCallV1(**_valid_kwargs(**{field: "A" * 64}))  # type: ignore[arg-type]


# (c) digest correctness: recorder over a canned call → digests equal locally recomputed
def test_recorder_digests_match_local_recomputation() -> None:
    normalized = {"action": "get_version"}
    request_payload: dict[str, object] = {
        "method": "tools/call",
        "params": {"name": "resolve_control"},
    }
    response_payload: dict[str, object] = {"connected": True, "version": "21.0.4.5"}

    # deterministic clock: start=1000, end=1001
    ticks = iter([1000, 1001])

    def clock() -> int:
        return next(ticks)

    recorder = McpCallRecorder(
        provider_version="2.98.3",
        resolve_version="21.0.4.5",
        server_mode="compound",
        clock=clock,
    )
    result, record = recorder.wrap_call(
        lambda: response_payload,
        tool_name="resolve_control",
        action="get_version",
        normalized_params=normalized,
        request_payload=request_payload,
    )
    assert result == response_payload
    assert record.normalized_params_sha256 == _canonical_sha(normalized)
    assert record.request_sha256 == _payload_sha(request_payload)
    assert record.response_sha256 == _payload_sha(response_payload)
    assert record.started_at == 1000
    assert record.finished_at == 1001
    assert record.status == "ok"

    # build_record with explicit payload types (bytes/str) also recomputable independently
    recorder2 = McpCallRecorder(
        provider_version="2.98.3",
        resolve_version="21.0.4.5",
        server_mode="compound",
        clock=lambda: 999,
    )
    raw_bytes = b'{"raw": true}'
    raw_str = '{"hello": "world"}'
    r2 = recorder2.build_record(
        tool_name="echo",
        action="echo",
        normalized_params={"x": 1},
        request_payload=raw_bytes,
        response_payload=raw_str,
        status="ok",
        started_at=10,
        finished_at=11,
    )
    assert r2.normalized_params_sha256 == _canonical_sha({"x": 1})
    assert r2.request_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert r2.response_sha256 == hashlib.sha256(raw_str.encode()).hexdigest()


# (d) append_call_record → file grows by exactly one canonical line, prior lines byte-identical
def test_append_is_append_only_and_byte_identical(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    first = McpExecutionCallV1(**_valid_kwargs(started_at=1, finished_at=2))  # type: ignore[arg-type]
    second = McpExecutionCallV1(
        **_valid_kwargs(  # type: ignore[arg-type]
            tool_name="echo",
            action="echo",
            started_at=3,
            finished_at=4,
            status="error",
            request_sha256="d" * 64,
            response_sha256="e" * 64,
        )
    )
    path = append_call_record(first, ledger_dir)
    assert path.exists()
    after_first = path.read_bytes()
    first_line = canonical_model_bytes(first) + b"\n"
    assert after_first == first_line

    append_call_record(second, ledger_dir)
    after_second = path.read_bytes()
    lines = after_second.splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == first_line
    assert lines[1] == canonical_model_bytes(second) + b"\n"
    assert read_call_records(ledger_dir) == (first, second)
    # stale_state probe: byte-identity held
    assert after_second[: len(first_line)] == first_line


# (e) aggregate counts correct
def test_aggregate_counts_correct() -> None:
    records = (
        McpExecutionCallV1(**_valid_kwargs(started_at=10, finished_at=12, tool_name="resolve_control", status="ok")),  # type: ignore[arg-type]  # noqa: E501
        McpExecutionCallV1(**_valid_kwargs(started_at=5, finished_at=9, tool_name="echo", status="error", request_sha256="d" * 64, response_sha256="e" * 64)),  # type: ignore[arg-type]  # noqa: E501
        McpExecutionCallV1(**_valid_kwargs(started_at=15, finished_at=20, tool_name="resolve_control", status="timeout", request_sha256="f" * 64, response_sha256="0" * 64)),  # type: ignore[arg-type]  # noqa: E501
        McpExecutionCallV1(**_valid_kwargs(started_at=8, finished_at=8, tool_name="echo", status="ok", request_sha256="1" * 64, response_sha256="2" * 64)),  # type: ignore[arg-type]  # noqa: E501
    )
    report = aggregate_calls(records)
    assert report.schema_version == "mcp-execution-report-v1"
    assert report.total_calls == 4
    assert report.by_status == {"ok": 2, "error": 1, "timeout": 1}
    assert report.by_tool == {"resolve_control": 2, "echo": 2}
    assert report.window_started_at == 5
    assert report.window_finished_at == 20

    empty = aggregate_calls(())
    assert empty.total_calls == 0
    assert empty.by_status == {}
    assert empty.by_tool == {}
    assert empty.window_started_at is None
    assert empty.window_finished_at is None
