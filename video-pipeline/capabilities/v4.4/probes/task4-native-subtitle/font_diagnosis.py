# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 4 font diagnosis: which Text+ input binds a Japanese-capable font?

Bounded single session on a disposable project. Steps:
  1. card timeline + insert_fusion_title + dump Template input IDs (io probe)
  2. set StyledText (Japanese) + candidate font input values, read each back
  3. render the card timeline alone, extract a frame
Evidence -> capabilities/v4.4/probes/task4-native-subtitle/font-diagnosis.json
All paths repo-relative; renders land under the evidence tree.
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
FONT_CANDIDATES = (
    "Hiragino Sans",
    "Hiragino Sans GB",
    "Hiragino Mincho ProN",
    "Yu Gothic",
    "Osaka",
)
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
        raise RuntimeError(f"{label} failed: {json.dumps(payload)[:400]}")
    return payload


def open_session() -> LiveSession:
    pin = load_mcp_pin(live_support.PIN_PATH)
    config = StdioTransportConfig.from_pin(pin, request_timeout_seconds=60.0)
    client = McpClient(StdioJsonRpcTransport(config))
    client.connect()
    media = generate_parity_media(Path(tempfile.gettempdir()) / f"t4font-{int(time.time())}")
    version = client.resolve_get_version()
    session = live_support.LiveSession(
        ops=None,  # type: ignore[arg-type]
        client=client,
        media=media,
        project_name="probe-t4font",
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
    hits = sorted(p for p in RENDER_DIR.glob(f"{name}*"))
    if not hits:
        raise RuntimeError(f"render {name} produced no file")
    return hits[-1]


def frame_png(video: Path, out: Path) -> None:
    for argv in (
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", "select='eq(n,10)'", "-frames:v", "1", str(out)],
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-frames:v", "1", str(out)],
    ):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        if done.returncode == 0 and out.is_file():
            return
    raise RuntimeError(f"frame extraction failed for {video}: {done.stderr[-200:]}")


def _prepared_card(session: LiveSession, name: str) -> str:
    """Disposable project + card timeline, or a loud failure."""
    if not session.ops.prepare_project(name, 30).ok:
        raise RuntimeError(f"prepare_project failed for {name}")
    card = f"{name}-card"
    if not session.ops.ensure_timeline(card).ok:
        raise RuntimeError(f"ensure_timeline failed for {card}")
    return card


def main() -> int:  # noqa: PLR0915 (frozen linear diagnostic procedure; one recorded session)
    live_support.PROBES_DIR = EVIDENCE
    live_support.LEDGER_DIR = EVIDENCE / "ledger"
    live_support.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    session = open_session()
    name = f"probe-t4font-{time.strftime('%H%M%S')}"
    report: dict[str, object] = {}
    try:
        _prepared_card(session, name)
        must(raw(session, "timeline", "insert_fusion_title", {"name": "Text+"}), "insert title")
        scope = {"timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0}}

        probe = raw(
            session, "fusion_comp", "probe_fusion_tool",
            {**scope, "tool_name": "Template", "include_io": True},
        )
        tool_block = probe.get("tool", {})
        inputs = tool_block.get("inputs", []) if isinstance(tool_block, dict) else []
        font_ids = [
            i.get("id") or i.get("name") for i in inputs
            if isinstance(i, dict)
            and "font" in str(i.get("id", "")).lower() + str(i.get("name", "")).lower()
        ]
        report["template_input_count"] = len(inputs)
        report["font_input_ids"] = font_ids
        report["all_input_ids"] = [i.get("id") for i in inputs if isinstance(i, dict)]

        must(
            raw(session, "fusion_comp", "safe_set_inputs",
                {**scope, "tool_name": "Template",
                 "inputs": {"StyledText": TEXT, "Size": 0.12}, "readback": True}),
            "set StyledText",
        )
        before_png = RENDER_DIR / "font-default.png"
        frame_png(render_current(session, "t4font-default"), before_png)
        input_ids = [i.get("id") for i in inputs if isinstance(i, dict)]
        font_id = "Font" if "Font" in input_ids else "FontName"
        report["font_input_used"] = font_id
        report["default_font_readback"] = raw(
            session, "fusion_comp", "get_input",
            {**scope, "tool_name": "Template", "input_name": font_id},
        )
        attempts: list[dict[str, object]] = []
        chosen: dict[str, object] | None = None
        for candidate in FONT_CANDIDATES:
            res = raw(
                session,
                "fusion_comp",
                "safe_set_inputs",
                {**scope, "tool_name": "Template",
                 "inputs": {font_id: candidate}, "readback": True},
            )
            readback = res.get("results", {}).get(font_id, {})
            attempts.append({"font": candidate, "response": readback})
            if res.get("success") and readback.get("value") == candidate:
                chosen = {"font": candidate, "font_input_id": font_id}
                break
        report["font_attempts"] = attempts
        report["chosen_font"] = chosen
        if chosen is None:
            report["verdict"] = "NO_JAPANESE_FONT_BINDABLE"
            (EVIDENCE / "font-diagnosis.json").write_text(
                json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"
            )
            print(json.dumps(report, indent=1, ensure_ascii=False, default=str)[:1600])
            return 3
        after_png = RENDER_DIR / "font-bound.png"
        frame_png(render_current(session, "t4font-bound"), after_png)
        report["rendered"] = {
            "default_frame": before_png.name,
            "bound_frame": after_png.name,
        }
        report["verdict"] = "RENDERED_FRAMES_CAPTURED_FOR_VISUAL_CHECK"
    except Exception as exc:  # noqa: BLE001 (diagnosis report on any failure)
        report["verdict"] = f"DIAGNOSIS_ERROR: {type(exc).__name__}: {exc}"
    finally:
        session.project_name = name
        with contextlib.suppress(Exception):
            live_support.close_live_session(session, delete_project=True)
    (EVIDENCE / "font-diagnosis.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"
    )
    tail = {k: report.get(k) for k in ("font_input_used", "chosen_font", "verdict", "rendered")}
    print(json.dumps(tail, ensure_ascii=False, default=str))
    if str(report.get("verdict")).startswith("DIAGNOSIS_ERROR"):
        return 1
    return 0 if report.get("verdict") == "RENDERED_FRAMES_CAPTURED_FOR_VISUAL_CHECK" else 3


if __name__ == "__main__":
    raise SystemExit(main())
