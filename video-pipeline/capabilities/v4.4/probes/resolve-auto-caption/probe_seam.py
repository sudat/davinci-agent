# noqa: INP001 (evidence tree is not an importable package by design)
"""Shared seam types, constants, sinks, and the disposal contract.

The disposal contract is the heart of the disposable-session guarantee:
``dispose`` deletes the project (proving deletion by load refusal) and
ONLY THEN closes the MCP session — ``close`` runs in a ``finally`` so it
happens after cleanup on every path, including exceptions, timeouts,
Ctrl-C, and partial readbacks. A pre-existing project is never deleted.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import caption_logic as logic

TIMELINE_NAME = "probe-autocap-tl"
FPS = 30000 / 1001
RUN_LABELS = ("r1", "r2")
OP_TIMEOUT_S = 60.0
IMPORT_TIMEOUT_S = 300.0
GENERATE_TIMEOUT_S = 900.0


class ActionSeam(Protocol):
    """One raw ``(tool, action, params)`` MCP call returning a dict."""

    def __call__(self, tool: str, action: str, params: dict[str, object],
                 timeout_seconds: float = OP_TIMEOUT_S) -> dict[str, object]: ...


class DisposableSession(Protocol):
    """A session owns one action seam and an explicit ``close``."""

    @property
    def call(self) -> ActionSeam: ...

    closed: bool

    def close(self) -> None: ...


class Sink(Protocol):
    """Raw-artifact sink: private writer or the in-memory selfcheck stand-in."""

    def write(self, name: str, payload: object) -> None: ...


@dataclass(frozen=True, slots=True)
class MediaInput:
    """The authoritative input media reference (path stays private)."""

    path: str
    expected_sha256: str
    actual_sha256: str


class PrivateSink:
    """Writes raw (prose-bearing) artifacts under the ignored private dir."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def write(self, name: str, payload: object) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


class NullSink:
    """Selfcheck sink: collects names in memory, writes nothing."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def write(self, name: str, payload: object) -> None:  # noqa: ARG002 (sink contract)
        self.names.append(name)


def ok_payload(payload: dict[str, object]) -> bool:
    """Vendor-ok: no error envelope and not an explicit success=false."""
    return (not isinstance(payload.get("error"), dict)
            and payload.get("success") is not False)


def classify_exception(exc: BaseException) -> str:
    """Map any failure to a closed verdict label (type names only, no prose)."""
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, logic.ProbeRefusalError):
        return exc.reason
    if type(exc).__name__ == logic.TIMEOUT_TYPE_NAME:
        return "timeout"
    return "error"


def project_name_for(run_label: str) -> str:
    return f"probe-resolve-autocap-{run_label}"


def cleanup(call: ActionSeam, project_name: str) -> dict[str, object]:
    """Delete the disposable project; PROVE deletion by load refusal."""
    deleted = call("project_manager", "delete", {"name": project_name})
    delete_ok = ok_payload(deleted) and deleted.get("success") is True
    after = call("project_manager", "load", {"name": project_name})
    return {"delete_success": delete_ok, "load_after_delete_ok": ok_payload(after)}


def dispose(session: DisposableSession, project_name: str,
            *, skip_delete_reason: str | None = None) -> dict[str, object]:
    """Cleanup FIRST, close LAST — close always runs, even if cleanup raises."""
    try:
        if skip_delete_reason is not None:
            return {"delete_success": False, "load_after_delete_ok": None,
                    "skipped_reason": skip_delete_reason}
        return cleanup(session.call, project_name)
    finally:
        session.close()


def dispose_safely(session: DisposableSession, project_name: str,
                   *, skip_delete_reason: str | None = None,
                   ) -> tuple[dict[str, object], str | None]:
    """``dispose`` with typed capture: ``(receipt, cleanup_failure_label)``.

    A cleanup exception must never abort the caller's ``finally`` bookkeeping
    (the session close already ran inside ``dispose``). The exception is
    reduced to a closed verdict label — no prose, no traceback — so a
    counts-only failure report can still be written.
    """
    try:
        return (dispose(session, project_name,
                        skip_delete_reason=skip_delete_reason), None)
    except BaseException as exc:  # noqa: BLE001 (closed-label capture only)
        label = classify_exception(exc)
        return ({"delete_success": False, "load_after_delete_ok": None,
                 "cleanup_failure": label}, label)


@dataclass
class FlowState:
    """Mutable per-session state shared by the phase functions."""

    call: ActionSeam
    sink: Sink
    media: MediaInput
    run_label: str
    report: dict[str, object] = field(default_factory=dict)
    phases: dict[str, object] = field(default_factory=dict)
    failure: str | None = None
    t0: float = field(default_factory=time.monotonic)
    timeline_start: int = 0
    clip_id: str = ""
    source_frames: int = 0
    chosen: str | None = None

    def phase(self, name: str) -> None:
        timed_phase(self.t0, name, self.phases)
        self.report["phases_ms"] = dict(self.phases)

    def fail(self, reason: str, where: str) -> None:
        if self.failure is None:
            self.failure = reason
            self.phase(where)


def timed_phase(t0: float, name: str, phases: dict[str, object]) -> None:
    """Record one cumulative phase elapsed marker (float monotonic basis)."""
    phases[name] = {"elapsed_ms": round((time.monotonic() - t0) * 1000, 1)}


__all__ = [
    "FPS",
    "GENERATE_TIMEOUT_S",
    "IMPORT_TIMEOUT_S",
    "OP_TIMEOUT_S",
    "RUN_LABELS",
    "TIMELINE_NAME",
    "ActionSeam",
    "DisposableSession",
    "FlowState",
    "MediaInput",
    "NullSink",
    "PrivateSink",
    "Sink",
    "classify_exception",
    "cleanup",
    "dispose",
    "dispose_safely",
    "ok_payload",
    "project_name_for",
    "timed_phase",
]
