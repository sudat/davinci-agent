# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 4 micro-probe 3: does binding Font break the render, or was it job-state drift?

Arms (one disposable project, fresh card timeline each, distinct render names):
  B: StyledText(Japanese) + Font="Hiragino Sans"          -> render FIRST
  A: StyledText(Japanese), default font                   -> render SECOND (control)
  C: StyledText(Japanese) + Font="Hiragino Sans W3"       -> render THIRD (alt name)
Per arm: probe_render_settings before the job, ffprobe streams after, frame PNG.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
sys.path.insert(0, str(VIDEO_PIPELINE))
sys.path.insert(0, str(VIDEO_PIPELINE / "tests" / "mcp_client"))

import live_support  # noqa: E402 (path bootstrap must precede imports)

from services.mcp_client.call_models import McpCallRecorder  # noqa: E402
from services.mcp_client.client import McpClient  # noqa: E402
from services.mcp_client.ops import McpOps  # noqa: E402
from services.mcp_client.transport import (  # noqa: E402
    StdioJsonRpcTransport,
    StdioTransportConfig,
)
from services.qa.parity_mcp import generate_parity_media  # noqa: E402
from services.toolchain.mcp_pin import load_mcp_pin  # noqa: E402

if TYPE_CHECKING:
    from live_support import LiveSession

TEXT = "日本語字幕テスト PROBE"
RENDER_DIR = EVIDENCE / "render"
_COMPLETE_PERCENT = 100.0


def raw(
    session: LiveSession, tool: str, action: str, params: dict[str, object]
) -> dict[str, object]:
    payload = session.client._call_action_json(tool, action, params)  # noqa: SLF001 (evidence capture seam)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{tool}.{action} non-dict: {payload!r}")  # noqa: TRY004 (response-shape guard, not an argument type)
    return payload


def must(payload: dict[str, object], label: str) -> dict[str, object]:
    if payload.get("success") is False or isinstance(payload.get("error"), dict):
        raise RuntimeError(f"{label} failed: {json.dumps(payload)[:300]}")
    return payload


def open_session() -> LiveSession:
    pin = load_mcp_pin(live_support.PIN_PATH)
    config = StdioTransportConfig.from_pin(pin, request_timeout_seconds=60.0)
    client = McpClient(StdioJsonRpcTransport(config))
    client.connect()
    media = generate_parity_media(Path(tempfile.gettempdir()) / f"t4m3-{int(time.time())}")
    version = client.resolve_get_version()
    session = live_support.LiveSession(
        ops=None,  # type: ignore[arg-type]
        client=client,
        media=media,
        project_name="probe-t4m3",
        resolve_version_string=version.version_string,
    )
    session.ops = McpOps(client)
    recorder = McpCallRecorder(
        provider_version=version.mcp.version,
        resolve_version=version.version_string,
        server_mode=pin.server_mode,
    )
    client._call_tool = live_support._CapturingCallTool(client._call_tool, session, recorder)  # noqa: SLF001 (evidence capture seam: the recorder wraps the client's private call hook to ledger every vendor call)
    return session


def render_current(session: LiveSession, name: str) -> Path:
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    for stale in RENDER_DIR.glob(f"{name}*"):
        stale.unlink(missing_ok=True)
    job = must(
        raw(
            session,
            "render",
            "prepare_render_job",
            {
                "target_dir": str(RENDER_DIR),
                "custom_name": name,
                "format": "mp4",
                "codec": "h264",
                "require_temp_target": False,
                "settings": {"ExportVideo": True, "DataBurnIn": "None"},
            },
        ),
        "prepare_render_job",
    )
    must(raw(session, "render", "start", {"job_ids": [job["job_id"]]}), "start")
    deadline = time.monotonic() + 150.0
    while time.monotonic() < deadline:
        status = raw(session, "render", "get_job_status", {"job_id": job["job_id"]})
        finished = float(status.get("CompletionPercentage") or 0) >= _COMPLETE_PERCENT
        if finished and not status.get("IsRenderingInProgress"):
            break
        time.sleep(4.0)
    hits = sorted(RENDER_DIR.glob(f"{name}*"))
    if not hits:
        raise RuntimeError(f"render {name} produced no file")
    return hits[-1]


def streams_of(video: Path) -> list[tuple[str, object]]:
    proc = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", str(video)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return [(s["codec_type"], s.get("nb_frames")) for s in json.loads(proc.stdout)["streams"]]


def frame_png(video: Path, out: Path) -> str:
    for argv in (
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", "select='eq(n,10)'", "-frames:v", "1", str(out)],
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-frames:v", "1", str(out)],
    ):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        if done.returncode == 0 and out.is_file():
            return out.name
    return f"EXTRACTION_FAILED: {done.stderr[-120:]}"


def run_arm(
    session: LiveSession, arm_prefix: str, tag: str, font: str | None
) -> dict[str, object]:
    card = f"{arm_prefix}-{tag}"
    must(raw(session, "media_pool", "create_timeline", {"name": card}), "create card")
    must(raw(session, "timeline", "set_current", {"name": card}), "current card")
    must(raw(session, "timeline", "insert_fusion_title", {"name": "Text+"}), "insert title")
    inputs: dict[str, object] = {"StyledText": TEXT, "Size": 0.12}
    if font is not None:
        inputs["Font"] = font
    must(
        raw(session, "fusion_comp", "safe_set_inputs",
            {"timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0},
             "tool_name": "Template", "inputs": inputs, "readback": True}),
        "set inputs",
    )
    font_readback = raw(
        session, "fusion_comp", "get_input",
        {"timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0},
         "tool_name": "Template", "input_name": "Font"},
    )
    settings_before = raw(session, "render", "probe_render_settings", {})
    video = render_current(session, f"t4m3-{tag}")
    streams = streams_of(video)
    has_video = any(kind == "video" for kind, _ in streams)
    frame = frame_png(video, RENDER_DIR / f"t4m3-{tag}.png") if has_video else "NO_VIDEO_STREAM"
    return {
        "font_requested": font,
        "font_readback": font_readback.get("value"),
        "settings_before": {
            k: settings_before.get(k)
            for k in ("settings", "is_rendering")
        },
        "render_file": video.name,
        "streams": streams,
        "frame": frame,
    }


def _prepared(name: str, session: LiveSession) -> None:
    """Disposable project ready, or a loud failure."""
    if not session.ops.prepare_project(name, 30).ok:
        raise RuntimeError(f"prepare_project failed for {name}")


def main() -> int:
    live_support.PROBES_DIR = EVIDENCE
    live_support.LEDGER_DIR = EVIDENCE / "ledger"
    live_support.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    session = open_session()
    name = f"probe-t4m3-{time.strftime('%H%M%S')}"
    report: dict[str, object] = {"schema_version": "task4-font-render-arms-v1", "text": TEXT}
    try:
        _prepared(name, session)
        arm_prefix = f"{name}-card"
        report["arm_B_hiragino_first"] = run_arm(session, arm_prefix, "B", "Hiragino Sans")
        report["arm_A_default_second"] = run_arm(session, arm_prefix, "A", None)
        report["arm_C_hiragino_w3"] = run_arm(session, arm_prefix, "C", "Hiragino Sans W3")
        report["verdict"] = "ARMS_CAPTURED"
    except Exception as exc:  # noqa: BLE001 (report on any failure)
        report["verdict"] = f"ERROR: {type(exc).__name__}: {exc}"
    finally:
        session.project_name = name
        with contextlib.suppress(Exception):
            live_support.close_live_session(session, delete_project=True)
    (EVIDENCE / "font-render-arms.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"
    )
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str)[:2400])
    return 0 if report.get("verdict") == "ARMS_CAPTURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
