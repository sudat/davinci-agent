# noqa: INP001 (evidence tree is not an importable package by design)
"""Rendered-span proof for Task 4: per-frame diffs gated to the cue spans.

Renders the same media with and without the overlay cues (two timelines),
extracts control/cue frame PNGs with ffmpeg, and asserts the per-frame
difference is present ONLY inside cue spans. The PNG pairs stay in the
evidence tree for visual glyph audit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from evidence_io import EVIDENCE, RENDER_DIR
from mcp_call import must, raw
from probe_session import CONTROL_FRAMES, CUE_FRAMES, CUES, ProbeState
from probe_steps import build_media_track
from render_freshness import render_once

if TYPE_CHECKING:
    from live_support import LiveSession


#: Frame-diff gate: a cue frame must differ at least this much, a control
#: frame (codec noise only) must stay below it. Evidence acceptance threshold.
_DIFF_GATE = 0.5


def frame_png(video: Path, frame: int, out: Path) -> None:
    subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", f"select='eq(n,{frame})'", "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=60, check=True,
    )


def frame_diff_yavg(a: Path, b: Path) -> float:
    proc = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-i", str(a), "-i", str(b), "-filter_complex",
         "[0:v][1:v]blend=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    line = next((row for row in proc.stdout.splitlines() if "YAVG" in row), "")
    return float(line.split("=")[-1]) if "=" in line else -1.0


def _diff_pair(
    control: Path, cue: Path, frame: int, diffs: dict[str, float]
) -> None:
    ca, cb = RENDER_DIR / f"ctl-n{frame}.png", RENDER_DIR / f"cue-n{frame}.png"
    frame_png(control, frame, ca)
    frame_png(cue, frame, cb)
    diffs[str(frame)] = frame_diff_yavg(ca, cb)


def probe_d_render(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    name = state["project_name"]
    reimported = session.ops.safe_import_media([str(session.media["parity-src-002"])])
    if reimported.ok and reimported.clips:
        state["clip_id"] = reimported.clips[0].clip_id
    ctl_tl = f"{name}-ctl"
    must(raw(session, "media_pool", "create_timeline", {"name": ctl_tl}), "create control tl")
    build_media_track(session, state, ctl_tl)
    control = render_once(session, "t4r-control", ctl_tl)
    cue = render_once(session, "t4r-cue", state["timeline_name"])
    diffs: dict[str, float] = {}
    for frame in CONTROL_FRAMES:
        _diff_pair(control, cue, frame, diffs)
    for frame in CUE_FRAMES.values():
        _diff_pair(control, cue, frame, diffs)
    inside = [diffs[str(f)] for f in CUE_FRAMES.values()]
    outside = [diffs[str(f)] for f in CONTROL_FRAMES]
    proof = {
        "schema_version": "task4-rendered-span-proof-v2",
        "cues": [dict(c) for c in CUES],
        "frame_diffs_yavg": diffs,
        "control_frames": list(CONTROL_FRAMES),
        "cue_frames": CUE_FRAMES,
        "renders": {
            "control": f"render/{control.name}",
            "cue": f"render/{cue.name}",
        },
    }
    (EVIDENCE / "rendered-span-proof.json").write_text(
        json.dumps(proof, indent=1, ensure_ascii=False) + "\n"
    )
    ok = all(v > _DIFF_GATE for v in inside) and all(v < _DIFF_GATE for v in outside)
    return ("ok" if ok else "failed"), (
        f"inside={inside} outside={outside} "
        "(PNGs render/cue-n*.png kept for visual glyph audit)"
    )


__all__ = ["frame_diff_yavg", "frame_png", "probe_d_render"]
