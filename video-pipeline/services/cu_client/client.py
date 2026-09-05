"""metacua-go child-process client: goal runs + the single-writer lease window.

metacua-go (Computer Use agent) is driven ONLY as a child process through
its non-interactive CLI surface: ``agent --goal <goal>`` to run, and
``sessions --limit N --json`` afterwards to find OUR session record by an
EXACT goal-string match (refinement 3: ``--limit 1`` can grab suda's
concurrent manual sessions — our lease only serializes OUR side, so the
record must be matched, not assumed newest).

§1.1 discipline: this package is justified by suda's direct instruction
plus measured necessity (CU-only capabilities exist — capability index
S05/S06/S07/S09 etc.); it is NOT a precedent for other abstraction layers,
and carries no app restrictions or step limits by suda's explicit
instruction.

Timeout design (refinement 2): a timeout kills the agent's whole process
group (``start_new_session=True`` + ``os.killpg`` SIGKILL) and then STILL
collects the session trace (the ``sessions`` lookup spawns its own
short-lived process, unaffected by the kill), returning a ``CuResult`` with
``exit_code=None``, ``verified="unverified"``, and a verification note
recording the interruption. The fact "may have interrupted mid-operation"
must never be recorded as a plain unverified ("didn't look") — they are
different facts.

Trace impermanence (refinement 4): ``trace_dir`` points under
``~/.metacua/traces/<goal_id>/``, OUTSIDE this repo. Traces are external
mutable state that may disappear; they are NOT captured artifacts and no
copy mechanism exists by design (size) — we do not imply durability we
cannot guarantee.
"""

# SIZE_OK: the approved design fixes this package at exactly four files
# (__init__/models/errors/client), so the lease window + goal runner live
# together here; the lines above the 250 ceiling are the design-mandated
# docstrings (§1.1 discipline, timeout design, trace impermanence) — pure
# code is 231 lines.

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.cu_client.errors import (
    CuLaunchError,
    CuLeaseError,
    CuTraceCollectionError,
)
from services.cu_client.models import CuPin, CuResult
from services.job_runner.stage_runner import STAGE_LEASE_TTL_SECONDS, stage_resource
from services.job_runner.state_errors import StateStoreError

if TYPE_CHECKING:
    # Annotation-only (methods are reached through the store object itself).
    from services.contracts.primitives import Identifier
    from services.job_runner.state_leases import LeaseOps

DEFAULT_CU_PIN_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "toolchains" / "cu-metacua.pin.json"
)
"""Committed pin; the environment-dependent binary path lives there, never in code."""

type CuVerifier = Callable[[CuResult], bool]
"""Caller-supplied verification: True -> verified; False or raise -> failed."""

_TRACE_TAIL_CHARACTERS: Final = 4000
_SESSIONS_LOOKUP_TIMEOUT_SECONDS: Final = 60.0
_INTERRUPTED_NOTE_PREFIX: Final = "interrupted:"
CU_WINDOW_RESOURCE: Final = stage_resource("cu-window", "metacua-goal")
"""Default lease resource; production callers pass their own stage_resource()."""


def _load_pin(pin_path: Path) -> CuPin:
    """Boundary-parse the pin; every failure is a typed CuLaunchError."""
    try:
        payload = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CuLaunchError(f"cannot read cu pin file {pin_path}: {exc}") from exc
    try:
        return CuPin.model_validate(payload)
    except ValidationError as exc:
        raise CuLaunchError(f"cu pin file {pin_path} violates cu-pin-v1: {exc}") from exc


def _tail(text: str) -> str:
    """Keep the LAST tail characters — run logs are diagnostics, not artifacts."""
    if len(text) <= _TRACE_TAIL_CHARACTERS:
        return text
    return text[-_TRACE_TAIL_CHARACTERS:]


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group (start_new_session made it leader)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _record_goal_id(record: dict[str, object]) -> str | None:
    value = record.get("goal_id")
    return value if isinstance(value, str) else None


def _record_finish(record: dict[str, object]) -> bool | None:
    value = record.get("finish")
    return value if isinstance(value, bool) else None


def _record_trace_dir(record: dict[str, object]) -> Path | None:
    images = record.get("images")
    if not isinstance(images, dict):
        return None
    value = images.get("dir")
    return Path(value) if isinstance(value, str) else None


def _select_session_record(
    binary: Path, lookup_limit: int, goal: str
) -> dict[str, object]:
    """Run ``sessions --limit N --json`` and return OUR record (exact goal match)."""
    argv = [str(binary), "sessions", "--limit", str(lookup_limit), "--json"]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SESSIONS_LOOKUP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CuTraceCollectionError(f"sessions lookup failed: {exc}") from exc
    if completed.returncode != 0:
        raise CuTraceCollectionError(
            f"sessions lookup exited {completed.returncode}: {_tail(completed.stderr)}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CuTraceCollectionError(f"sessions output is not valid JSON: {exc}") from exc
    records: object = payload
    if isinstance(payload, dict):
        records = payload.get("sessions")
    if not isinstance(records, list):
        raise CuTraceCollectionError(
            "sessions output is neither a list nor an object with a 'sessions' list"
        )
    for record in records:
        if not isinstance(record, dict):
            raise CuTraceCollectionError("sessions output contains a non-object record")
        if record.get("goal") == goal:
            return record
    raise CuTraceCollectionError(
        f"no session with an exact goal match within the last {lookup_limit} sessions"
    )


def _apply_verification(result: CuResult, verify: CuVerifier | None) -> CuResult:
    """Observation contract: 未観測 unless the verifier proves otherwise."""
    if verify is None:
        return result
    try:
        passed = verify(result)
    except Exception as exc:  # noqa: BLE001 (contract: a raising verifier is a failed verification, not an abort)
        return result.model_copy(
            update={
                "verified": "failed_verification",
                "verification_note": f"verify raised {type(exc).__name__}: {exc}",
            }
        )
    if passed:
        return result.model_copy(update={"verified": "verified"})
    return result.model_copy(
        update={
            "verified": "failed_verification",
            "verification_note": "verify returned False",
        }
    )


def _was_interrupted(result: CuResult) -> bool:
    note = result.verification_note
    return note is not None and note.startswith(_INTERRUPTED_NOTE_PREFIX)


class CuClient:
    """Thin typed client over the pinned metacua-go CLI (mirrors mcp_client style).

    The pin is loaded ONCE at construction (fail-closed on any config
    drift); the binary's existence and executability are checked at CALL
    time because the wrapper can appear/disappear on the machine between
    construction and use.
    """

    def __init__(self, *, pin_path: Path | str = DEFAULT_CU_PIN_PATH) -> None:
        self._pin = _load_pin(Path(pin_path))

    @property
    def pin(self) -> CuPin:
        return self._pin

    def _launchable_binary(self) -> Path:
        binary = Path(self._pin.binary_path).expanduser()
        if not binary.is_file():
            raise CuLaunchError(f"metacua-go binary not found at {binary}")
        if not os.access(binary, os.X_OK):
            raise CuLaunchError(f"metacua-go binary is not executable: {binary}")
        return binary

    def run_goal(
        self,
        goal: str,
        *,
        timeout_s: float | None = None,
        extra_flags: Sequence[str] = (),
        verify: CuVerifier | None = None,
    ) -> CuResult:
        """Run one goal non-interactively and collect its session trace.

        ``timeout_s`` defaults to the pin's ``default_timeout_s`` (3600:
        max-steps 400 + effort high means legitimate goals run very long;
        a shorter default would kill legitimate work — pass a shorter
        value explicitly when the goal is known to be quick). ``extra_flags``
        are appended verbatim (e.g. ``("--allow-bash",)``); this client adds
        no flag of its own and no allowlist.

        On timeout the agent's process GROUP is SIGKILLed, the trace is
        still collected, and the returned ``CuResult`` records the
        interruption — verify is NOT run in that case (external GUI state
        may be mid-operation, so a readback would mislead).
        """
        timeout = self._pin.default_timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError(f"timeout_s must be positive, got {timeout}")
        binary = self._launchable_binary()
        argv = [str(binary), "agent", "--goal", goal, *extra_flags]
        started = time.monotonic()
        try:
            proc: subprocess.Popen[str] = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise CuLaunchError(f"metacua-go spawn failed: {exc}") from exc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
        elapsed = time.monotonic() - started

        record = _select_session_record(binary, self._pin.session_lookup_limit, goal)
        result = CuResult(
            goal=goal,
            exit_code=None if timed_out else proc.returncode,
            goal_id=_record_goal_id(record),
            finish=_record_finish(record),
            trace_dir=_record_trace_dir(record),
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            elapsed_seconds=elapsed,
            verified="unverified",
            verification_note=(
                f"{_INTERRUPTED_NOTE_PREFIX} killed by timeout after {elapsed:.1f}s;"
                " external GUI state may be mid-operation"
                if timed_out
                else None
            ),
        )
        if timed_out:
            return result
        return _apply_verification(result, verify)


def _lease_guard(call: str, op: Callable[[], object]) -> None:
    """Run one LeaseOps call, converting refusal into a typed CuLeaseError."""
    try:
        op()
    except StateStoreError as exc:
        raise CuLeaseError(f"cu_window {call} refused: {exc}") from exc


def cu_window(  # noqa: PLR0913 (the handoff contract's knobs are the signature)
    *,
    store: LeaseOps,
    holder: Identifier,
    goal: str,
    resource: Identifier = CU_WINDOW_RESOURCE,
    ttl_seconds: int = STAGE_LEASE_TTL_SECONDS,
    timeout_s: float | None = None,
    extra_flags: Sequence[str] = (),
    verify: CuVerifier | None = None,
    client: CuClient | None = None,
) -> CuResult:
    """Single-writer handoff: release OUR lease to the CU agent, run, retake it.

    Fixed order (refinement 1): renew_lease (prove we hold it) ->
    release_lease (explicit handoff) -> run_goal -> FINALLY acquire_lease to
    take the writer role back. The reacquire runs EVEN IF run_goal raised,
    so no orphaned nobody-holds-the-lock state remains; if another holder
    took the lease meanwhile, that is a typed CuLeaseError (fail fast —
    never wait). Verification runs only AFTER the reacquire, reading back
    under OUR lease; an interrupted (timeout) run keeps its interruption
    note and skips verification.
    """
    runner = client if client is not None else CuClient()
    now = int(time.time())
    _lease_guard(
        "renew_lease",
        lambda: store.renew_lease(
            resource=resource, holder=holder, now=now, ttl_seconds=ttl_seconds
        ),
    )
    _lease_guard(
        "release_lease",
        lambda: store.release_lease(resource=resource, holder=holder, now=now),
    )
    try:
        result = runner.run_goal(goal, timeout_s=timeout_s, extra_flags=extra_flags)
    finally:
        _lease_guard(
            "acquire_lease",
            lambda: store.acquire_lease(
                resource=resource,
                holder=holder,
                now=int(time.time()),
                ttl_seconds=ttl_seconds,
            ),
        )
    if _was_interrupted(result):
        return result
    return _apply_verification(result, verify)


__all__ = [
    "CU_WINDOW_RESOURCE",
    "DEFAULT_CU_PIN_PATH",
    "CuClient",
    "CuVerifier",
    "cu_window",
]
