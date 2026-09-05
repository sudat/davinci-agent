"""Offline metacua-go stub: installable binary + recording lease store.

The stub is a tiny python script written per-test into ``tmp_path`` (with
its own canned ``sessions`` JSON beside it), so no real metacua-go, GUI,
or DaVinci Resolve is ever contacted. Env knobs (``STUB_AGENT_SLEEP``,
``STUB_STDOUT_PAD``) are read by the stub process through normal
subprocess environment inheritance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_leases import LeaseOps
from services.job_runner.state_models import LeaseRow

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier

STUB_SCRIPT = """\
#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

SESSIONS_FILE = Path(__file__).with_name("stub-sessions.json")


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    if sys.argv[1] == "agent":
        goal = sys.argv[sys.argv.index("--goal") + 1]
        print("stub-agent argv: " + " ".join(sys.argv))
        print("stub-agent goal: " + goal)
        pad = int(os.environ.get("STUB_STDOUT_PAD", "0"))
        if pad:
            print("x" * pad)
        sleep_s = float(os.environ.get("STUB_AGENT_SLEEP", "0"))
        if sleep_s:
            time.sleep(sleep_s)
        return 0
    if sys.argv[1] == "sessions":
        sys.stdout.write(SESSIONS_FILE.read_text(encoding="utf-8"))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
"""


def install_stub(
    tmp_path: Path,
    sessions: object,
    *,
    binary_path: Path | None = None,
) -> Path:
    """Write stub binary + canned sessions JSON + cu-pin; return the pin path."""
    binary = binary_path if binary_path is not None else tmp_path / "metacua-stub"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(STUB_SCRIPT, encoding="utf-8")
    binary.chmod(0o755)
    (tmp_path / "stub-sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    pin_path = tmp_path / "cu-metacua.pin.json"
    pin_path.write_text(
        json.dumps(
            {
                "schema_version": "cu-pin-v1",
                "binary_path": str(binary),
                "default_timeout_s": 120,
                "session_lookup_limit": 8,
            }
        ),
        encoding="utf-8",
    )
    return pin_path


def session_record(
    goal: str, goal_id: str, *, finish: bool, trace_dir: Path | None = None
) -> dict[str, object]:
    """One metacua-go ``sessions`` record as consumed by the client."""
    return {
        "goal": goal,
        "goal_id": goal_id,
        "finish": finish,
        "images": {
            "dir": str(trace_dir if trace_dir is not None else Path("~/.metacua/traces") / goal_id)
        },
    }


class RecordingLeaseStore(LeaseOps):
    """Fake LeaseOps recording the call order, with per-op refusal switches."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resources: list[str] = []
        self.refuse_renew = False
        self.refuse_release = False
        self.refuse_acquire = False

    def acquire_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        self.calls.append("acquire_lease")
        self.resources.append(resource)
        if self.refuse_acquire:
            raise StateStoreError(
                "lease-held",
                f"lease {resource} is held by a fresh holder (now {now})",
            )
        return LeaseRow(resource=resource, holder=holder, expires_at=now + ttl_seconds)

    def renew_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        self.calls.append("renew_lease")
        self.resources.append(resource)
        if self.refuse_renew:
            raise StateStoreError("not-holder", f"lease {resource} is held by another")
        return LeaseRow(resource=resource, holder=holder, expires_at=now + ttl_seconds)

    def release_lease(
        self, *, resource: Identifier, holder: Identifier, now: int
    ) -> None:
        self.calls.append("release_lease")
        self.resources.append(resource)
        if self.refuse_release:
            raise StateStoreError("not-holder", f"lease {resource} is held by another")


__all__ = ["RecordingLeaseStore", "install_stub", "session_record"]
