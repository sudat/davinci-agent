"""Shared machinery for the task-11 live session (probes / parity / flags).

Everything here runs ONLY under ``-m mcp_live`` against the real pinned
server and a live Resolve.  The module provides:

- media generation via :func:`services.qa.parity_mcp.generate_parity_media`
  (tiny ffmpeg ``testsrc2`` MP4s under /tmp — never inside the repo).
- :class:`LiveSession` — one pinned server + typed :class:`McpOps` with the
  task-8 recorder attached so every ``tools/call`` lands in the append-only
  call ledger, and a raw-step capture list for per-probe evidence logs.
- :func:`run_probe` — one capability probe: bounded, evidence-logged, never
  aborting the batch (a failing capability records ``status=failed`` and
  the batch continues).
- :func:`write_matrix_and_snapshot` — fills ``capabilities/v4.4/mcp-fit.json``
  and emits the ``mcp-capability-snapshot-v1`` artifact whose hash verifies
  against the final matrix bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from services.mcp_client.call_models import (
    McpCallRecorder,
    append_call_record,
)
from services.mcp_client.client import McpClient, McpToolResult
from services.mcp_client.errors import McpTimeoutError
from services.mcp_client.ops import McpOps
from services.mcp_client.ops_models import McpActionOutcome
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig
from services.qa.parity_mcp import generate_parity_media
from services.toolchain.mcp_pin import load_mcp_pin

VIDEO_PIPELINE_ROOT: Final = Path(__file__).resolve().parents[2]
PIN_PATH: Final = VIDEO_PIPELINE_ROOT / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
V44_RUNS_DIR: Final = VIDEO_PIPELINE_ROOT / "capabilities" / "v4.4" / "runs"
PROBES_DIR: Final = V44_RUNS_DIR / "probes"
LEDGER_DIR: Final = PROBES_DIR / "ledger"
MCP_FIT_PATH: Final = VIDEO_PIPELINE_ROOT / "capabilities" / "v4.4" / "mcp-fit.json"
SNAPSHOT_PATH: Final = V44_RUNS_DIR / "mcp-capability-snapshot.json"


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n"


def matrix_snapshot_hash(matrix_bytes: bytes) -> str:
    """Hash definition of ``mcp-capability-snapshot-v1.capability_snapshot_hash``.

    sha256 over the CANONICAL JSON of the matrix (sort_keys + compact
    separators, default ``ensure_ascii``), NOT over the raw file bytes —
    the on-disk matrix uses ``indent=2``, so a raw-byte hash would change
    on every reformat.  The live writer (``write_matrix_and_snapshot``) and
    the offline verification test MUST share this one function.
    """
    canonical = json.dumps(
        json.loads(matrix_bytes), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class LiveSession:
    """One live server session with recorder + raw step capture attached."""

    ops: McpOps
    client: McpClient
    media: dict[str, Path]
    project_name: str
    steps: list[dict[str, object]] = field(default_factory=list)
    resolve_version_string: str = ""

    def fail(self, outcome: McpActionOutcome, what: str) -> tuple[str, str]:
        """Uniform (status, readback) for a failed typed outcome."""
        error = outcome.error
        detail = error.message if error is not None else "success=false"
        code = error.code if error is not None else "no-error-envelope"
        return "failed", f"{what}: {detail} (code={code})"


ProbeState = dict[str, str]
ProbeFn = Callable[[LiveSession, ProbeState], tuple[str, str]]


class _CapturingCallTool:
    """Wrap ``McpClient._call_tool``: ledger row + raw step per tools/call."""

    def __init__(
        self,
        original: Callable[..., McpToolResult],
        session: LiveSession,
        recorder: McpCallRecorder,
    ) -> None:
        self._original = original
        self._session = session
        self._recorder = recorder

    def __call__(
        self, name: str, arguments: Mapping[str, object], **kwargs: object
    ) -> McpToolResult:
        action = str(arguments.get("action", "")) if isinstance(arguments, Mapping) else ""
        params = arguments.get("params", {}) if isinstance(arguments, Mapping) else {}
        started_logical = int(time.time())
        started_wall = time.monotonic()
        status = "ok"
        response_text: str | None = None
        result: McpToolResult | None = None
        raised: BaseException | None = None
        try:
            result = self._original(name, arguments, **kwargs)
            response_text = result.text
        except McpTimeoutError as exc:
            status, raised = "timeout", exc
            response_text = json.dumps({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 (ledger row for every failure mode)
            status, raised = "error", exc
            response_text = json.dumps({"error": str(exc)})
        record = self._recorder.build_record(
            tool_name=name,
            action=action or "-",
            normalized_params=dict(params) if isinstance(params, Mapping) else {},
            request_payload={"name": name, "arguments": dict(arguments)},
            response_payload=response_text or {"error": "no-response"},
            status=status,
            started_at=started_logical,
            finished_at=max(int(time.time()), started_logical),
        )
        append_call_record(record, LEDGER_DIR)
        elapsed = time.monotonic() - started_wall
        response: object = None
        if response_text is not None:
            with contextlib.suppress(json.JSONDecodeError):
                response = json.loads(response_text)
        error_block = response.get("error") if isinstance(response, dict) else None
        ok = status == "ok" and not isinstance(error_block, dict)
        self._session.steps.append(
            {
                "tool": name,
                "action": action,
                "params": params if isinstance(params, (dict, list)) else {},
                "ok": ok,
                "elapsed_ms": round(elapsed * 1000, 1),
                "response": response if response is not None else (response_text or "")[:2000],
                "status": status,
            }
        )
        if raised is not None:
            raise raised
        assert result is not None
        return result


def open_live_session(media_dir: Path, project_name: str) -> LiveSession:
    """Spawn the pinned server, verify identity, attach recorder + capture."""
    pin = load_mcp_pin(PIN_PATH)
    config = StdioTransportConfig.from_pin(pin)
    client = McpClient(StdioJsonRpcTransport(config))
    client.connect()
    media = generate_parity_media(media_dir)
    version = client.resolve_get_version()
    session = LiveSession(
        ops=McpOps(client),
        client=client,
        media=media,
        project_name=project_name,
        resolve_version_string=version.version_string,
    )
    recorder = McpCallRecorder(
        provider_version=version.mcp.version,
        resolve_version=version.version_string,
        server_mode=pin.server_mode,
    )
    client._call_tool = _CapturingCallTool(client._call_tool, session, recorder)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    PROBES_DIR.mkdir(parents=True, exist_ok=True)
    return session


def close_live_session(session: LiveSession, *, delete_project: bool = True) -> None:
    """Best-effort cleanup: delete the probe project, close the server."""
    if delete_project:
        with contextlib.suppress(Exception):
            session.ops._action(
                McpActionOutcome,
                "project_manager",
                "delete",
                {"name": session.project_name},
            )
    session.client.close()


def run_probe(
    session: LiveSession, capability: str, fn: ProbeFn, state: ProbeState | None = None
) -> dict[str, object]:
    """Run one capability probe; failures are recorded, never raised."""
    mark = len(session.steps)
    started = time.monotonic()
    status, readback = "failed", "probe raised before producing a verdict"
    try:
        status, readback = fn(session, state if state is not None else {})
    except Exception as exc:  # noqa: BLE001 (probe isolation: record and continue)
        status = "failed"
        readback = f"exception: {type(exc).__name__}: {exc}"
    log: dict[str, object] = {
        "capability": capability,
        "fixture": f"v43-{capability}-01",
        "status": status,
        "readback": readback,
        "wall_seconds": round(time.monotonic() - started, 3),
        "steps": session.steps[mark:],
    }
    (PROBES_DIR / f"{capability}.json").write_bytes(canonical_json_bytes(log))
    return log


def standard_visual_from_manifest(manifest: dict[str, object]) -> dict[str, object] | None:
    """Author the V2 visual object from the REAL shot_table of the pending payload."""
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        return None
    first = clips[0]
    if not isinstance(first, dict):
        return None
    visual_block = first.get("visual")
    if not isinstance(visual_block, dict):
        return None
    shot_table = visual_block.get("shot_table")
    if not isinstance(shot_table, list) or not shot_table:
        return None
    descriptions: list[dict[str, object]] = []
    for index, row in enumerate(shot_table, start=1):
        if not isinstance(row, dict):
            continue
        start = row.get("time_seconds_start") or row.get("start") or 0.0
        end = row.get("time_seconds_end") or row.get("end") or float(start) + 1.0
        descriptions.append(
            {
                "shot_index": row.get("shot_index", index),
                "time_seconds_start": float(start),
                "time_seconds_end": float(end),
                "frame_indices_used": row.get("frame_indices_used") or [1],
                "description": "Synthetic ffmpeg testsrc2 calibration pattern (host-authored)",
                "visual": {
                    "shot_size": "other",
                    "framing": "abstract",
                    "camera_motion": "locked",
                },
                "content": {
                    "action": "Static test pattern with moving color bars.",
                    "location": "Generated media.",
                },
                "editorial": {
                    "editorial_role": "coverage",
                    "select_potential": "low",
                    "best_moment_present": False,
                    "best_moment": None,
                    "pacing": "flat",
                    "stillness_type": None,
                },
                "cuttability": {
                    "cut_in": {"quality": "clean", "notes": ""},
                    "cut_out": {"quality": "clean", "notes": ""},
                    "match_action_in": False,
                    "match_action_out": False,
                },
                "confidence": {
                    "visual": "high",
                    "content": "high",
                    "audio": "high",
                    "editorial": "medium",
                    "cuttability": "medium",
                },
            }
        )
    if not descriptions:
        return None
    return {
        "success": True,
        "provider": "host_chat_paths",
        "schema_version": "2.0",
        "clip_summary": "Synthetic calibration clip (ffmpeg testsrc2) for capability probes.",
        "clip_summary_oneliner": "Synthetic test pattern clip.",
        "editorial_classification": {
            "primary_use": "other",
            "select_potential": "low",
            "energy_arc": "flat",
            "style": "experimental",
            "genre_indicators": [],
            "reason": "Machine-generated calibration media.",
        },
        "slate": {
            "slate_visible": False,
            "scene": "",
            "shot": "",
            "take": "",
            "camera": "",
            "roll": "",
            "date": "",
            "production": "",
            "visible_text": [],
            "confidence": {
                "overall": "high",
                "scene": "high",
                "shot": "high",
                "take": "high",
                "camera": "high",
            },
        },
        "shot_descriptions": descriptions,
    }


def _deep_shots_from_payload(payload_shots: list[dict[str, object]]) -> list[dict[str, object]]:
    shots: list[dict[str, object]] = []
    for row in payload_shots[:2]:
        if not isinstance(row, dict):
            continue
        shots.append(
            {
                "shot_index": row.get("shot_index"),
                "shot_uuid": row.get("shot_uuid"),
                "frame_indices": row.get("frame_indices") or [0],
                "visual": {
                    "shot_size": "other",
                    "framing": "abstract",
                    "camera_motion": "locked",
                },
                "content": {
                    "primary_subject": {
                        "type": "other",
                        "description": "Synthetic test pattern (host-authored)",
                    },
                    "action": "Static calibration pattern.",
                    "location": "Generated media.",
                },
                "description": "Deep pass over synthetic test pattern (host-authored).",
                "editorial": {
                    "editorial_role": "coverage",
                    "select_potential": "low",
                    "best_moment_present": False,
                    "best_moment": None,
                    "pacing": "flat",
                    "stillness_type": None,
                },
                "cuttability": {
                    "cut_in": {"quality": "clean", "notes": ""},
                    "cut_out": {"quality": "clean", "notes": ""},
                    "match_action_in": False,
                    "match_action_out": False,
                },
                "confidence": {
                    "visual": "high",
                    "content": "high",
                    "audio": "high",
                    "editorial": "medium",
                    "cuttability": "medium",
                },
            }
        )
    return shots


def deep_shots_from_payload(payload_shots: list[dict[str, object]]) -> list[dict[str, object]]:
    """Author the deep pass for EVERY payload shot: the vision token covers
    exactly the shots the confirmed deepen payload offered, so committing a
    subset would fail the token check."""
    shots: list[dict[str, object]] = []
    for row in payload_shots:
        if not isinstance(row, dict):
            continue
        shots.append(
            {
                "shot_index": row.get("shot_index"),
                "shot_uuid": row.get("shot_uuid"),
                "frame_indices": row.get("frame_indices") or [0],
                "visual": {
                    "shot_size": "other",
                    "framing": "abstract",
                    "camera_motion": "locked",
                },
                "content": {
                    "primary_subject": {
                        "type": "other",
                        "description": "Synthetic test pattern (host-authored)",
                    },
                    "action": "Static calibration pattern.",
                    "location": "Generated media.",
                },
                "description": "Deep pass over synthetic test pattern (host-authored).",
                "editorial": {
                    "editorial_role": "coverage",
                    "select_potential": "low",
                    "best_moment_present": False,
                    "best_moment": None,
                    "pacing": "flat",
                    "stillness_type": None,
                },
                "cuttability": {
                    "cut_in": {"quality": "clean", "notes": ""},
                    "cut_out": {"quality": "clean", "notes": ""},
                    "match_action_in": False,
                    "match_action_out": False,
                },
                "confidence": {
                    "visual": "high",
                    "content": "high",
                    "audio": "high",
                    "editorial": "medium",
                    "cuttability": "medium",
                },
            }
        )
    return shots


def matrix_evidence_ref(capability: str) -> str:
    log_path = PROBES_DIR / f"{capability}.json"
    digest = hashlib.sha256(log_path.read_bytes()).hexdigest()
    return f"{log_path.relative_to(VIDEO_PIPELINE_ROOT)}#sha256={digest}"


def write_matrix_and_snapshot(
    probe_logs: Sequence[dict[str, object]],
    *,
    provider_version: str,
    pin_commit: str,
    server_mode: str,
    resolve_build: str,
) -> Path:
    """Fill mcp-fit.json rows from probe logs; emit the snapshot artifact."""
    by_capability = {str(log["capability"]): log for log in probe_logs}
    matrix_payload: dict[str, object] = json.loads(MCP_FIT_PATH.read_bytes())
    rows = matrix_payload["capabilities"]
    assert isinstance(rows, list)
    for row in rows:
        capability = str(row["capability"])
        log = by_capability[capability]
        row["status"] = log["status"]
        row["readback"] = str(log["readback"])[:400]
        row["evidence_refs"] = [matrix_evidence_ref(capability)]
    matrix_bytes = json.dumps(
        matrix_payload, sort_keys=True, indent=2, ensure_ascii=False
    ).encode("utf-8") + b"\n"
    MCP_FIT_PATH.write_bytes(matrix_bytes)
    snapshot_hash = matrix_snapshot_hash(matrix_bytes)
    snapshot = {
        "schema_version": "mcp-capability-snapshot-v1",
        "provider": "davinci-resolve-mcp",
        "provider_version": provider_version,
        "resolve_build": resolve_build,
        "server_mode": server_mode,
        "capability_snapshot_hash": snapshot_hash,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pin_commit": pin_commit,
    }
    SNAPSHOT_PATH.write_bytes(canonical_json_bytes(snapshot))
    return SNAPSHOT_PATH


__all__ = [
    "LEDGER_DIR",
    "MCP_FIT_PATH",
    "PROBES_DIR",
    "SNAPSHOT_PATH",
    "V44_RUNS_DIR",
    "VIDEO_PIPELINE_ROOT",
    "LiveSession",
    "canonical_json_bytes",
    "close_live_session",
    "deep_shots_from_payload",
    "matrix_evidence_ref",
    "matrix_snapshot_hash",
    "open_live_session",
    "run_probe",
    "standard_visual_from_manifest",
    "write_matrix_and_snapshot",
]
