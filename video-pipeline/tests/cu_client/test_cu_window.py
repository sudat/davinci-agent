"""cu_window single-writer handoff contract against the offline stub.

Covers: the fixed renew -> release -> run -> reacquire -> verify order,
the finally-guaranteed reacquire when run_goal raises (refinement 1),
fail-fast CuLeaseError on every refused lease step, and deferred
verification running only after the writer role is back in OUR hands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.cu_client.client import CU_WINDOW_RESOURCE, CuClient, cu_window
from services.cu_client.errors import CuLaunchError, CuLeaseError
from tests.cu_client.stub_metacua import (
    RecordingLeaseStore,
    install_stub,
    session_record,
)

if TYPE_CHECKING:
    from services.cu_client.models import CuResult

GOAL = "stabilize the hero clip"


def _client(tmp_path: Path) -> CuClient:
    pin_path = install_stub(tmp_path, [session_record(GOAL, "g-win", finish=True)])
    return CuClient(pin_path=pin_path)


def test_window_order_renew_release_run_acquire_then_verify(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    store.calls.clear()
    client = _client(tmp_path)

    def _verify(_result: CuResult) -> bool:
        store.calls.append("verify")
        return True

    result = cu_window(
        store=store, holder="job-runner:1", goal=GOAL, verify=_verify, client=client
    )
    assert store.calls == ["renew_lease", "release_lease", "acquire_lease", "verify"]
    assert result.verified == "verified"
    assert result.goal_id == "g-win"
    assert result.exit_code == 0


def test_window_default_resource_is_the_cu_window_stage(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    cu_window(store=store, holder="h", goal=GOAL, client=_client(tmp_path))
    assert store.resources == [CU_WINDOW_RESOURCE] * 3


def test_window_run_goal_raising_still_reacquires(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    pin_path = tmp_path / "cu-metacua.pin.json"
    pin_path.write_text(
        json.dumps(
            {
                "schema_version": "cu-pin-v1",
                "binary_path": str(tmp_path / "nowhere" / "metacua-go"),
                "default_timeout_s": 60,
                "session_lookup_limit": 8,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CuLaunchError):
        cu_window(store=store, holder="h", goal=GOAL, client=CuClient(pin_path=pin_path))
    assert store.calls == ["renew_lease", "release_lease", "acquire_lease"]


def test_window_reacquire_refusal_is_fail_fast_cu_lease_error(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    store.refuse_acquire = True
    with pytest.raises(CuLeaseError, match="acquire_lease refused"):
        cu_window(store=store, holder="h", goal=GOAL, client=_client(tmp_path))
    assert store.calls == ["renew_lease", "release_lease", "acquire_lease"]


def test_window_renew_refusal_never_releases_or_runs(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    store.refuse_renew = True
    with pytest.raises(CuLeaseError, match="renew_lease refused"):
        cu_window(store=store, holder="h", goal=GOAL, client=_client(tmp_path))
    assert store.calls == ["renew_lease"]


def test_window_release_refusal_never_runs_and_never_reacquires(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    store.refuse_release = True
    with pytest.raises(CuLeaseError, match="release_lease refused"):
        cu_window(store=store, holder="h", goal=GOAL, client=_client(tmp_path))
    assert store.calls == ["renew_lease", "release_lease"]


def test_window_verify_false_and_raising_are_failed_verification(tmp_path: Path) -> None:
    store = RecordingLeaseStore()
    client = _client(tmp_path)

    failed = cu_window(
        store=store, holder="h", goal=GOAL, verify=lambda _r: False, client=client
    )
    assert failed.verified == "failed_verification"
    assert failed.verification_note == "verify returned False"

    def _boom(_r: CuResult) -> bool:
        raise RuntimeError("resolve readback mismatch")

    raised = cu_window(store=store, holder="h", goal=GOAL, verify=_boom, client=client)
    assert raised.verified == "failed_verification"
    assert raised.verification_note is not None
    assert "resolve readback mismatch" in raised.verification_note
    # Each full window leaves the lease back in OUR holder's hands.
    assert store.calls.count("acquire_lease") == 2
