"""Single-writer execution runner guard — task 10 TDD."""

from __future__ import annotations

import json
from pathlib import Path

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
    # validate early via model to ensure correct shape
    BackendsConfig.model_validate(cfg)
    path.write_text(json.dumps(cfg))
    return path


def _open_store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.sqlite3")


def _fake_transport(calls: list[tuple[str, str, object]]):
    def _transport(tool_name: str, action: str, params: object):
        calls.append((tool_name, action, params))
        return {"ok": True, "tool": tool_name}

    return _transport


def test_lease_held_mutating_call_proceeds(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "mcp")
    clock = SequenceClock(start=1000)
    job_id = "job-1"
    stage = "stage-a"
    holder = "holder-1"
    store.acquire_lease(
        resource=stage_resource(job_id, stage),
        holder=holder,
        now=clock.now,
        ttl_seconds=60,
    )
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage,
        holder_token=holder,
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
        transport=_fake_transport(calls),
    )
    result = runner.execute(
        tool_name="resolve_control",
        action="create_timeline",
        normalized_params={"action": "create_timeline"},
        request_payload={"action": "create_timeline"},
    )
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "resolve_control"
    records = read_call_records(ledger)
    assert len(records) == 1
    assert records[0].status == "ok"
    assert records[0].tool_name == "resolve_control"
    store.close()


def test_no_lease_mutating_raises_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "mcp")
    clock = SequenceClock(start=2000)
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id="job-1",
        stage_name="stage-a",
        holder_token="holder-1",  # noqa: S106
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
        transport=_fake_transport(calls),
    )
    with pytest.raises(NotLeaseHolderError) as exc:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
            request_payload={"action": "create_timeline"},
        )
    assert exc.value.code == "not-lease-holder"
    assert "not-lease-holder" in str(exc.value)
    assert len(calls) == 0
    records = read_call_records(ledger)
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].tool_name == "resolve_control"
    assert records[0].action == "create_timeline"
    store.close()


def test_legacy_backend_mutating_refused_and_audited(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "legacy_direct")
    clock = SequenceClock(start=3000)
    job_id = "job-1"
    stage = "stage-a"
    holder = "holder-1"
    store.acquire_lease(
        resource=stage_resource(job_id, stage),
        holder=holder,
        now=clock.now,
        ttl_seconds=60,
    )
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage,
        holder_token=holder,
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
        transport=_fake_transport(calls),
    )
    with pytest.raises(BackendPolicyError) as exc:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    assert exc.value.code == "backend-policy"
    assert len(calls) == 0
    records = read_call_records(ledger)
    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].tool_name == "resolve_control"
    store.close()


def test_read_only_under_legacy_allowed(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "legacy_direct")
    clock = SequenceClock(start=4000)
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id="job-1",
        stage_name="stage-a",
        holder_token="holder-1",  # noqa: S106
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
        transport=_fake_transport(calls),
    )
    result = runner.execute(
        tool_name="resolve_control",
        action="get_version",
        normalized_params={"action": "get_version"},
        read_only=True,
    )
    assert result["ok"] is True
    assert len(calls) == 1
    records = read_call_records(ledger)
    assert len(records) == 1
    assert records[0].status == "ok"
    assert records[0].tool_name == "resolve_control"
    store.close()


def test_assisted_mode_mutating_against_production_refused(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "mcp")
    clock = SequenceClock(start=5000)
    job_id = "prod-job-1"
    stage = "stage-a"
    holder = "holder-1"
    store.acquire_lease(
        resource=stage_resource(job_id, stage),
        holder=holder,
        now=clock.now,
        ttl_seconds=60,
    )
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage,
        holder_token=holder,
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
        assisted_mode=True,
        transport=_fake_transport(calls),
    )
    with pytest.raises(AssistedModeError) as exc:
        runner.execute(
            tool_name="resolve_control",
            action="create_timeline",
            normalized_params={"action": "create_timeline"},
        )
    assert exc.value.code == "assisted-mode-production-refused"
    assert len(calls) == 0
    records = read_call_records(ledger)
    assert len(records) == 1
    assert records[0].status == "error"
    store.close()


def test_happy_path_lease_mcp_recorded_with_ok(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    ledger = tmp_path / "ledger"
    backends_path = _write_backends(tmp_path / "backends.json", "mcp")
    clock = SequenceClock(start=6000)
    job_id = "job-1"
    stage = "stage-a"
    holder = "holder-1"
    store.acquire_lease(
        resource=stage_resource(job_id, stage),
        holder=holder,
        now=clock.now,
        ttl_seconds=60,
    )
    calls: list[tuple[str, str, object]] = []
    runner = McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage,
        holder_token=holder,
        ledger_dir=ledger,
        backends_path=backends_path,
        clock=clock,
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
    assert result["ok"] is True
    records = read_call_records(ledger)
    assert len(records) == 1
    rec = records[0]
    assert rec.status == "ok"
    assert rec.tool_name == "echo"
    assert rec.action == "echo"
    assert rec.provider_version == "2.98.3"
    assert rec.readback_refs == ()
    assert rec.started_at <= rec.finished_at
    # ledger is append-only: second read equals first
    assert read_call_records(ledger) == records
    # assisted non-production allowed (prefix dev-)
    # need lease for dev job as well
    dev_holder = "holder-dev"
    dev_store_clock = SequenceClock(start=7000)
    # use same store but different job resource
    store.acquire_lease(
        resource=stage_resource("dev-job-2", stage),
        holder=dev_holder,
        now=dev_store_clock.now,
        ttl_seconds=60,
    )
    dev_runner2 = McpExecutionRunner(
        store=store,
        job_id="dev-job-2",
        stage_name=stage,
        holder_token=dev_holder,
        ledger_dir=tmp_path / "ledger2",
        backends_path=backends_path,
        clock=dev_store_clock,
        assisted_mode=True,
        transport=_fake_transport([]),
    )
    res2 = dev_runner2.execute(
        tool_name="echo",
        action="echo",
        normalized_params={"x": 2},
    )
    assert res2["ok"] is True
    store.close()
