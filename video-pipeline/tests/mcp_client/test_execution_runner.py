"""Single-writer execution runner guard — task 10 TDD.

Every timing value is logical (``SequenceClock``); no test sleeps.
The lease fixture reuses ``StateStore.acquire_lease`` exactly as
``tests/job_runner`` does — the runner must verify ownership through
the same public LeaseOps API, never raw SQL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from services.config.backends import BackendsConfig
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import read_call_records
from services.mcp_client.execution_runner import (
    AssistedModeError,
    BackendPolicyError,
    McpExecutionRunner,
    NotLeaseHolderError,
)


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


def _open_store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.sqlite3")


def _fake_transport(calls: list[tuple[str, str, object]]) -> Callable[..., Any]:
    def _transport(tool_name: str, action: str, params: object) -> dict[str, object]:
        calls.append((tool_name, action, params))
        return {"ok": True, "tool": tool_name}

    return _transport


def _make_runner(  # noqa: PLR0913 (fixture wiring is explicit)
    tmp_path: Path,
    store: StateStore,
    *,
    job_id: str = "job-1",
    stage_name: str = "stage-a",
    holder_token: str = "holder-1",  # noqa: S107 (lease token, not a credential)
    execution_backend: str = "mcp",
    clock: SequenceClock | None = None,
    assisted_mode: bool = False,
    transport_calls: list[tuple[str, str, object]] | None = None,
    ledger_dir: Path | None = None,
) -> McpExecutionRunner:
    resolved_clock = clock if clock is not None else SequenceClock(1000)
    return McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage_name,
        holder_token=holder_token,
        ledger_dir=ledger_dir if ledger_dir is not None else tmp_path / "ledger",
        backends_path=_write_backends(
            tmp_path / f"backends-{execution_backend}.json", execution_backend
        ),
        clock=resolved_clock,
        assisted_mode=assisted_mode,
        transport=_fake_transport(
            transport_calls if transport_calls is not None else []
        ),
    )


def _acquire(store: StateStore, job_id: str, stage_name: str, holder: str, now: int) -> None:
    store.acquire_lease(
        resource=stage_resource(job_id, stage_name), holder=holder, now=now, ttl_seconds=60
    )


# (a) lease held -> mutating call proceeds and is recorded ok.


def test_lease_held_mutating_call_proceeds(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "job-1", "stage-a", "holder-1", now=1000)
    runner = _make_runner(
        tmp_path, store, transport_calls=calls, clock=SequenceClock(1000)
    )
    result = runner.execute(
        tool_name="resolve_control",
        action="create_timeline",
        normalized_params={"action": "create_timeline"},
        request_payload={"action": "create_timeline"},
    )
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "resolve_control"
    assert calls[0][1] == "create_timeline"
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "ok"
    store.close()


# (b) no lease -> NotLeaseHolderError("not-lease-holder"), transport untouched,
# refusal audited in the ledger.


def test_no_lease_mutating_refused_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    runner = _make_runner(
        tmp_path, store, transport_calls=calls, clock=SequenceClock(2000)
    )
    with pytest.raises(NotLeaseHolderError) as exc_info:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    assert exc_info.value.code == "not-lease-holder"
    assert len(calls) == 0
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].tool_name == "resolve_control"
    assert records[0].action == "create_timeline"
    store.close()


# (c) execution_backend=legacy_direct -> mutating call refused, audited.


def test_legacy_backend_mutating_refused_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "job-1", "stage-a", "holder-1", now=3000)
    runner = _make_runner(
        tmp_path,
        store,
        execution_backend="legacy_direct",
        transport_calls=calls,
        clock=SequenceClock(3000),
    )
    with pytest.raises(BackendPolicyError) as exc_info:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    assert exc_info.value.code == "backend-policy"
    assert len(calls) == 0
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].tool_name == "resolve_control"
    store.close()


# (d) read_only call under legacy_direct -> allowed regardless of backend.


def test_read_only_under_legacy_allowed(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    runner = _make_runner(
        tmp_path,
        store,
        execution_backend="legacy_direct",
        transport_calls=calls,
        clock=SequenceClock(4000),
    )
    result = runner.execute(
        tool_name="resolve_control",
        action="get_version",
        normalized_params={"action": "get_version"},
        read_only=True,
    )
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert len(calls) == 1
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "ok"
    store.close()


# (e) assisted_mode mutating against a production job -> refused;
# the same mode against a dev-prefixed job passes.


def test_assisted_mode_mutating_against_production_refused(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "prod-job-1", "stage-a", "holder-1", now=5000)
    runner = _make_runner(
        tmp_path,
        store,
        job_id="prod-job-1",
        assisted_mode=True,
        transport_calls=calls,
        clock=SequenceClock(5000),
    )
    with pytest.raises(AssistedModeError) as exc_info:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    assert exc_info.value.code == "assisted-mode-production-refused"
    assert len(calls) == 0
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "error"
    store.close()


def test_assisted_mode_dev_prefix_job_allowed(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "dev-job-2", "stage-a", "holder-dev", now=5500)
    runner = _make_runner(
        tmp_path,
        store,
        job_id="dev-job-2",
        holder_token="holder-dev",  # noqa: S106 (lease token, not a credential)
        assisted_mode=True,
        transport_calls=calls,
        clock=SequenceClock(5500),
        ledger_dir=tmp_path / "ledger-dev",
    )
    result = runner.execute(
        tool_name="echo",
        action="echo",
        normalized_params={"x": 2},
    )
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert len(calls) == 1
    store.close()


# (f) happy path lease+mcp -> call recorded with status ok and full lineage.


def test_happy_path_lease_mcp_recorded_with_ok(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "job-1", "stage-a", "holder-1", now=6000)
    ledger = tmp_path / "ledger"
    runner = McpExecutionRunner(
        store=store,
        job_id="job-1",
        stage_name="stage-a",
        holder_token="holder-1",  # noqa: S106 (lease token, not a credential)
        ledger_dir=ledger,
        backends_path=_write_backends(tmp_path / "backends-mcp.json", "mcp"),
        clock=SequenceClock(6000),
        transport=_fake_transport(calls),
        provider_version="2.98.3",
        resolve_version="21.0.4",
        server_mode="compound",
    )
    result = runner.execute(
        tool_name="echo",
        action="echo",
        normalized_params={"x": 1},
        request_payload={"x": 1},
    )
    assert isinstance(result, dict)
    assert result["ok"] is True
    records = read_call_records(ledger)
    assert len(records) == 1
    record = records[0]
    assert record.status == "ok"
    assert record.tool_name == "echo"
    assert record.action == "echo"
    assert record.provider_version == "2.98.3"
    assert record.resolve_version == "21.0.4"
    assert record.server_mode == "compound"
    assert record.readback_refs == ()
    assert record.started_at <= record.finished_at
    # stale_state probe: re-read equals first read (append-only, no rewrite).
    assert read_call_records(ledger) == records
    store.close()


# Guard depth: expired lease and foreign holder are both refusals.


def test_expired_lease_mutating_refused_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "job-1", "stage-a", "holder-1", now=1000)
    clock = SequenceClock(1000)
    clock.advance(61)  # ttl was 60s: the lease row is now expired.
    runner = _make_runner(tmp_path, store, transport_calls=calls, clock=clock)
    with pytest.raises(NotLeaseHolderError) as exc_info:
        runner.execute(
            tool_name="resolve_control",
            action="append_clip",
            normalized_params={"action": "append_clip"},
        )
    assert exc_info.value.code == "not-lease-holder"
    assert len(calls) == 0
    records = read_call_records(tmp_path / "ledger")
    assert len(records) == 1
    assert records[0].status == "error"
    store.close()


def test_wrong_holder_mutating_refused_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    _acquire(store, "job-1", "stage-a", "holder-real", now=1000)
    runner = _make_runner(
        tmp_path,
        store,
        holder_token="holder-impostor",  # noqa: S106 (lease token, not a credential)
        transport_calls=calls,
        clock=SequenceClock(1000),
        ledger_dir=tmp_path / "ledger-impostor",
    )
    with pytest.raises(NotLeaseHolderError):
        runner.execute(
            tool_name="resolve_control",
            action="append_clip",
            normalized_params={"action": "append_clip"},
        )
    assert len(calls) == 0
    records = read_call_records(tmp_path / "ledger-impostor")
    assert len(records) == 1
    assert records[0].status == "error"
    store.close()


# stale_state probe: a refusal line stays byte-identical when a later
# successful call appends to the same ledger.


def test_ledger_append_only_across_refusal_then_success(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    calls: list[tuple[str, str, object]] = []
    ledger = tmp_path / "ledger"
    refusing = _make_runner(
        tmp_path, store, transport_calls=calls, clock=SequenceClock(7000), ledger_dir=ledger
    )
    with pytest.raises(NotLeaseHolderError):
        refusing.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    lines_before = (ledger / "mcp-call-ledger.jsonl").read_bytes().splitlines()
    assert len(lines_before) == 1

    _acquire(store, "job-1", "stage-a", "holder-1", now=7100)
    holding = _make_runner(
        tmp_path, store, transport_calls=calls, clock=SequenceClock(7100), ledger_dir=ledger
    )
    holding.execute(
        tool_name="echo",
        action="echo",
        normalized_params={"x": 1},
    )
    lines_after = (ledger / "mcp-call-ledger.jsonl").read_bytes().splitlines()
    assert len(lines_after) == 2
    assert lines_after[0] == lines_before[0]
    statuses = [record.status for record in read_call_records(ledger)]
    assert statuses == ["error", "ok"]
    store.close()
