# noqa: INP001 (evidence tree is not an importable package by design)
"""Live wiring for the disposable auto-caption probe (loaded only for runs).

Owns the pinned MCP session — ``McpClient.from_pin`` with the 900 s bound,
the existing ``McpCallRecorder`` ledger seam (digests only, ledger under
the private run dir), raw step capture, and the EXPLICIT ``close`` — plus
the one-shot ``live_run`` that drives ``run_flow`` and disposes (cleanup
first, close last) in a ``finally`` on every path. Importing this module
pulls in the live client stack.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
REPO = VIDEO_PIPELINE.parent
for _path in (VIDEO_PIPELINE, EVIDENCE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if TYPE_CHECKING:
    from services.mcp_client.call_models import McpCallRecorder
    from services.mcp_client.client import McpClient

import caption_logic as logic  # noqa: E402 (path bootstrap first — task4 pattern)
from probe_flow import run_flow  # noqa: E402 (path bootstrap first)
from probe_seam import (  # noqa: E402 (path bootstrap first)
    GENERATE_TIMEOUT_S,
    OP_TIMEOUT_S,
    RUN_LABELS,
    ActionSeam,
    DisposableSession,
    MediaInput,
    PrivateSink,
    Sink,
    classify_exception,
    dispose_safely,
    project_name_for,
)

PIN_PATH = VIDEO_PIPELINE / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
ASR_RUN_DIR = (REPO / "private" / "reference-episodes" / "v44-real-01" / "runs"
               / "probe-system-asr-r1")
PRIVATE_RUNS_DIR = REPO / "private" / "reference-episodes" / "v44-real-01" / "runs"


class LiveSession:
    """Pinned MCP client + recorder seam; ``close`` is explicit and idempotent."""

    def __init__(self, client: McpClient, recorder: McpCallRecorder,
                 ledger_dir: Path, identity: dict[str, object]) -> None:
        self._client = client
        self._recorder = recorder
        self._ledger_dir = ledger_dir
        self.identity = identity
        self.steps: list[dict[str, object]] = []
        self.closed = False

    @property
    def call(self) -> ActionSeam:
        return self._call

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def _call(self, tool: str, action: str, params: dict[str, object],
              timeout_seconds: float = OP_TIMEOUT_S) -> dict[str, object]:
        from services.mcp_client.call_models import (  # noqa: PLC0415 (live-only import)
            append_call_record,
        )
        started = int(time.time())
        mark = time.monotonic()
        status: str = "ok"
        raised: BaseException | None = None
        payload: object = None
        try:
            raw_call = self._client._call_action_json  # noqa: SLF001 (package-private raw seam — task4 pattern)
            payload = raw_call(tool, action, params,
                               timeout_seconds=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 (ledger row for every failure mode)
            status = ("timeout"
                      if type(exc).__name__ == logic.TIMEOUT_TYPE_NAME else "error")
            payload = {"error_type": type(exc).__name__}
            raised = exc
        record = self._recorder.build_record(
            tool_name=tool, action=action, normalized_params=dict(params),
            request_payload={"name": tool,
                             "arguments": {"action": action, "params": dict(params)}},
            response_payload=payload, status=status,
            started_at=started, finished_at=max(int(time.time()), started))
        append_call_record(record, self._ledger_dir)
        self.steps.append({"tool": tool, "action": action, "status": status,
                           "elapsed_ms": round((time.monotonic() - mark) * 1000, 1),
                           "response": payload})
        if raised is not None:
            raise raised
        if not isinstance(payload, dict):
            msg = f"{tool}.{action} non-dict payload"
            raise TypeError(msg)
        narrowed: dict[str, object] = dict(payload)
        return narrowed


def open_live_session(run_dir: Path) -> LiveSession:
    """Connect the pinned MCP server and arm the recorder seam."""
    from services.mcp_client.call_models import McpCallRecorder  # noqa: PLC0415 (live-only)
    from services.mcp_client.client import McpClient  # noqa: PLC0415 (live-only)
    from services.toolchain.mcp_pin import load_mcp_pin  # noqa: PLC0415 (live-only)

    pin = load_mcp_pin(PIN_PATH)
    client = McpClient.from_pin(PIN_PATH, request_timeout_seconds=GENERATE_TIMEOUT_S)
    client.connect()
    try:
        version = client.resolve_get_version()
    except BaseException:
        client.close()
        raise
    recorder = McpCallRecorder(
        provider_version=version.mcp.version,
        resolve_version=version.version_string,
        server_mode=pin.server_mode)
    identity: dict[str, object] = {
        "resolve_version": version.version_string,
        "mcp_version": version.mcp.version,
        "pin_commit": pin.commit, "server_mode": pin.server_mode}
    return LiveSession(client, recorder, run_dir / "ledger", identity)


def discover_media() -> MediaInput:
    """Authoritative input media from the ASR probe evidence (path private)."""
    bindings = sorted(ASR_RUN_DIR.glob("asr-cache/*/binding.json"))
    if len(bindings) != 1:
        print(f"verdict=media-record-ambiguous bindings={len(bindings)}", flush=True)
        raise SystemExit(1)
    binding = json.loads(bindings[0].read_text())
    return MediaInput(path=str(binding["media_path"]),
                      expected_sha256=str(binding["media_sha256"]),
                      actual_sha256="")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_with_disposal(report: dict[str, object],
                           session: DisposableSession,
                           steps: list[dict[str, object]],
                           sink: Sink, project_name: str) -> None:
    """The ``finally`` tail of ``live_run`` (extracted for selfcheck).

    Disposes with typed capture: a cleanup exception becomes a closed-label
    ``cleanup_failure`` receipt — the counts-only report and private steps
    are still written, and a successful flow verdict is NEVER left standing
    when cleanup failed (first failure wins if the flow already failed).
    """
    skip = str(report.get("verdict")) == "project-exists"
    receipt, cleanup_failure = dispose_safely(
        session, project_name,
        skip_delete_reason="project-exists" if skip else None)
    report["cleanup"] = receipt
    report["session_closed"] = session.closed
    if cleanup_failure is not None and str(report.get("verdict")) == "generated-and-read":
        report["verdict"] = cleanup_failure
        report["failure"] = {"reason": cleanup_failure, "phase": "cleanup"}
    sink.write("steps.json", steps)


def live_run(run_label: str) -> tuple[dict[str, object], int]:
    """One bounded disposable session; dispose (cleanup, then close) always."""
    run_dir = PRIVATE_RUNS_DIR / f"resolve-auto-caption-{run_label}"
    media = discover_media()
    media_file = Path(media.path)
    if not media_file.is_file():
        print(f"verdict=media-missing run={run_label}", flush=True)
        return {"verdict": "media-missing"}, 1
    media = MediaInput(path=media.path, expected_sha256=media.expected_sha256,
                       actual_sha256=sha256_file(media_file))
    session = open_live_session(run_dir)
    sink = PrivateSink(run_dir)
    project_name = project_name_for(run_label)
    report: dict[str, object] = {}
    exit_code = 0
    try:
        report = run_flow(session.call, sink, media, run_label)
    except BaseException as exc:  # noqa: BLE001 (classified; disposal follows)
        reason = classify_exception(exc)
        report = {"schema_version": "resolve-auto-caption-capability-v1",
                  "run": run_label, "project_name": project_name,
                  "input_media_sha256": media.expected_sha256,
                  "verdict": reason,
                  "failure": {"reason": reason, "phase": "exception"}}
        exit_code = 1
    finally:
        finalize_with_disposal(report, session, session.steps, sink, project_name)
    report.update(session.identity)
    report["input_media_bytes"] = media_file.stat().st_size
    logic.assert_sanitized(report)
    receipt = report["cleanup"]
    if not isinstance(receipt, dict):
        raise TypeError("cleanup receipt missing")
    if (str(report.get("verdict")) != "generated-and-read"
            or receipt.get("delete_success") is not True
            or receipt.get("load_after_delete_ok") is not False):
        exit_code = 1
    return report, exit_code


__all__ = [
    "PRIVATE_RUNS_DIR",
    "RUN_LABELS",
    "LiveSession",
    "discover_media",
    "live_run",
    "open_live_session",
    "sha256_file",
]
