"""verify_phase0a smoke observation honesty (blocker fix).

The smoke observation may never claim a confirmed loopback-only policy
while the scripting scope is unresolved; it must restate the LIVE-verified
scope exactly as the host report records it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.resolve_bridge.readiness import HostReadinessError
from services.toolchain.models import SmokeRecord, ToolchainLock, load_lock
from services.toolchain.verify_phase0a import verify_phase0a_smoke

FIXTURES = Path("tests/fixtures/resolve-host")
LOCK = Path("config/toolchains/phase-0a-v1.json")


def _lock_with_report(report_path: Path) -> ToolchainLock:
    # the frozen phase-0a lock IS the ToolchainLock variant
    lock = load_lock(LOCK)
    assert isinstance(lock, ToolchainLock)
    return lock.model_copy(
        update={
            "resolve": lock.resolve.model_copy(
                update={
                    "report_path": str(report_path.resolve()),
                    "report_sha256": sha256_file(report_path),
                }
            )
        }
    )


def test_unresolved_report_fails_smoke_without_false_confirmation() -> None:
    lock = _lock_with_report(FIXTURES / "safe-readonly.json")
    with pytest.raises(HostReadinessError, match="unverified"):
        verify_phase0a_smoke(lock, ("resolve-readonly",))


def test_live_loopback_report_states_verified_scope(tmp_path: Path) -> None:
    report = json.loads((FIXTURES / "live-loopback.json").read_text())
    report["output"]["cwd"] = str(Path.cwd().resolve())
    live = tmp_path / "live-loopback.json"
    live.write_text(json.dumps(report))
    lock = _lock_with_report(live)
    verified = verify_phase0a_smoke(lock, ("resolve-readonly",))
    observation = verified.smoke.resolve_readonly.observation
    assert "loopback-only policy confirmed" not in observation
    assert "scripting scope verified live: loopback" in observation
    assert verified.smoke.resolve_readonly.status == "passed"
    assert isinstance(verified.smoke.resolve_readonly, SmokeRecord)


def test_live_local_network_report_states_permit(tmp_path: Path) -> None:
    report = json.loads((FIXTURES / "live-local-network.json").read_text())
    report["output"]["cwd"] = str(Path.cwd().resolve())
    live = tmp_path / "live-local-network.json"
    live.write_text(json.dumps(report))
    lock = _lock_with_report(live)
    verified = verify_phase0a_smoke(lock, ("resolve-readonly",))
    observation = verified.smoke.resolve_readonly.observation
    assert "local-network" in observation
    assert "explicit operator permit" in observation
