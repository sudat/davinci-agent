"""CuClient.run_goal contract against the offline metacua-go stub.

Covers: launch refusals (missing / non-executable binary, bad pin config),
the happy path, exact-goal session selection among concurrent manual
sessions, trace-collection failures, the timeout kill of the process group
with the recorded interruption note, the caller-verifier observation
contract, output-tail truncation, and extra_flags pass-through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.cu_client.client import CuClient
from services.cu_client.errors import CuLaunchError, CuTraceCollectionError
from services.cu_client.models import CuResult
from tests.cu_client.stub_metacua import install_stub, session_record

GOAL = "open the project and add a marker at 01:00:00:00"


def _pin(
    tmp_path: Path, sessions: object, *, binary_path: Path | None = None
) -> Path:
    return install_stub(tmp_path, sessions, binary_path=binary_path)


def test_nonexistent_binary_path_refuses_launch(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "metacua-go"
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-1", finish=True)])
    pin_path.write_text(
        json.dumps(
            {
                "schema_version": "cu-pin-v1",
                "binary_path": str(missing),
                "default_timeout_s": 60,
                "session_lookup_limit": 8,
            }
        ),
        encoding="utf-8",
    )
    client = CuClient(pin_path=pin_path)
    with pytest.raises(CuLaunchError, match="not found"):
        client.run_goal(GOAL)


def test_non_executable_binary_refuses_launch(tmp_path: Path) -> None:
    binary = tmp_path / "metacua-stub-noexec"
    pin_path = _pin(
        tmp_path, [session_record(GOAL, "g-1", finish=True)], binary_path=binary
    )
    binary.chmod(0o644)
    with pytest.raises(CuLaunchError, match="not executable"):
        CuClient(pin_path=pin_path).run_goal(GOAL)


def test_happy_path_populates_result_fields(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces" / "g-ours"
    pin_path = _pin(
        tmp_path,
        [session_record(GOAL, "g-ours", finish=True, trace_dir=trace_dir)],
    )
    result = CuClient(pin_path=pin_path).run_goal(GOAL)
    assert isinstance(result, CuResult)
    assert result.goal == GOAL
    assert result.exit_code == 0
    assert result.goal_id == "g-ours"
    assert result.finish is True
    assert result.trace_dir == trace_dir
    assert "stub-agent goal: " + GOAL in result.stdout_tail
    assert result.elapsed_seconds >= 0
    assert result.verified == "unverified"
    assert result.verification_note is None


def test_concurrent_manual_session_first_still_selects_our_goal(tmp_path: Path) -> None:
    # Refinement 3: suda's concurrent manual session is the NEWEST record;
    # our lease only serializes OUR side, so the exact goal match decides.
    manual = session_record("suda is manually clicking in the GUI", "g-manual", finish=True)
    ours = session_record(GOAL, "g-ours", finish=False, trace_dir=tmp_path / "t-ours")
    pin_path = _pin(tmp_path, [manual, ours])
    result = CuClient(pin_path=pin_path).run_goal(GOAL)
    assert result.goal_id == "g-ours"
    assert result.finish is False
    assert result.trace_dir == tmp_path / "t-ours"


def test_extra_flags_are_passed_through_verbatim(tmp_path: Path) -> None:
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-1", finish=True)])
    result = CuClient(pin_path=pin_path).run_goal(GOAL, extra_flags=("--allow-bash",))
    assert "--allow-bash" in result.stdout_tail


def test_sessions_without_matching_goal_refuses_trace(tmp_path: Path) -> None:
    pin_path = _pin(
        tmp_path, [session_record("suda clicked something manually", "g-man", finish=True)]
    )
    with pytest.raises(CuTraceCollectionError, match="exact goal match"):
        CuClient(pin_path=pin_path).run_goal(GOAL)


def test_invalid_sessions_json_refuses_trace(tmp_path: Path) -> None:
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-1", finish=True)])
    (tmp_path / "stub-sessions.json").write_text("not json at all", encoding="utf-8")
    with pytest.raises(CuTraceCollectionError, match="not valid JSON"):
        CuClient(pin_path=pin_path).run_goal(GOAL)


def test_timeout_kills_process_group_and_records_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_AGENT_SLEEP", "30")
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-slow", finish=False)])
    result = CuClient(pin_path=pin_path).run_goal(GOAL, timeout_s=1.0)
    assert result.exit_code is None
    assert result.verified == "unverified"
    assert result.verification_note is not None
    assert "interrupted:" in result.verification_note
    assert "timeout" in result.verification_note
    assert "mid-operation" in result.verification_note
    assert 0.9 <= result.elapsed_seconds < 15
    assert result.goal_id == "g-slow"  # trace still collected after the kill
    # Reaching these asserts in ~1s while the stub slept 30s proves the kill.


def test_verify_contract_observation_states(tmp_path: Path) -> None:
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-1", finish=True)])
    client = CuClient(pin_path=pin_path)

    unverified = client.run_goal(GOAL)  # verify=None -> 未観測, never success
    assert unverified.verified == "unverified"
    assert unverified.verification_note is None

    verified = client.run_goal(GOAL, verify=lambda _result: True)
    assert verified.verified == "verified"
    assert verified.verification_note is None

    failed = client.run_goal(GOAL, verify=lambda _result: False)
    assert failed.verified == "failed_verification"
    assert failed.verification_note == "verify returned False"

    def _boom(_result: CuResult) -> bool:
        raise RuntimeError("readback saw no marker")

    raised = client.run_goal(GOAL, verify=_boom)
    assert raised.verified == "failed_verification"
    assert raised.verification_note is not None
    assert "RuntimeError" in raised.verification_note
    assert "readback saw no marker" in raised.verification_note


def test_timeout_skips_verify_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_AGENT_SLEEP", "30")
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-slow", finish=False)])
    calls: list[str] = []

    def _verify(_result: CuResult) -> bool:
        calls.append("verify")
        return True

    result = CuClient(pin_path=pin_path).run_goal(GOAL, timeout_s=0.5, verify=_verify)
    assert calls == []  # mid-operation state must not be read back as proof
    assert result.verified == "unverified"
    assert result.verification_note is not None
    assert result.verification_note.startswith("interrupted:")


def test_output_tails_truncated_sensibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_STDOUT_PAD", "20000")
    pin_path = _pin(tmp_path, [session_record(GOAL, "g-1", finish=True)])
    result = CuClient(pin_path=pin_path).run_goal(GOAL)
    assert len(result.stdout_tail) == 4000
    assert result.stdout_tail.endswith("\n")
    assert "x" * 500 in result.stdout_tail
    assert len(result.stderr_tail) <= 4000


def test_pin_missing_binary_path_key_is_typed_refusal(tmp_path: Path) -> None:
    pin_path = tmp_path / "cu-metacua.pin.json"
    pin_path.write_text(
        json.dumps(
            {
                "schema_version": "cu-pin-v1",
                "default_timeout_s": 60,
                "session_lookup_limit": 8,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CuLaunchError, match="cu-pin-v1"):
        CuClient(pin_path=pin_path)


def test_pin_missing_or_invalid_file_is_typed_refusal(tmp_path: Path) -> None:
    with pytest.raises(CuLaunchError, match="cannot read cu pin"):
        CuClient(pin_path=tmp_path / "absent.pin.json")
    bad = tmp_path / "bad.pin.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(CuLaunchError, match="cu-pin-v1"):
        CuClient(pin_path=bad)
