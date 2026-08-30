# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 4 final consolidated live probe (repair run, 2026-08-26).

Regenerates ALL evidence in this directory from one bounded session against
the real pinned MCP + Resolve 21.0.4.5:

  p1     session: disposable project + 30fps timeline + testsrc2 media
  p2     primary placement V1 [0,150)
  a      auto-caption expressibility verdict (path A cannot carry cues)
  b      ONE cue through the product adapter (LiveMcpAdapter) with the
         style-bound Japanese font; exact span + text + style readbacks
  b2     rerun through the product adapter: zero duplicates, identity kept
  c      ALL cues (incl. the long two-line cue): exact spans/texts/styles
  d      rendered proof: control vs cue timelines, per-frame diffs gated to
         the cue spans; before/during/after PNGs for visual audit
  prod   independent McpOps readback + verdict; product-run.json

Module map: ``mcp_call`` (raw typed actions) / ``evidence_io`` (paths,
resets, scrub) / ``render_freshness`` (poll-or-fail + selfcheck) /
``probe_session`` (live bootstrap + cues) / ``probe_steps`` (probe
sequence) / ``render_evidence`` (rendered-span proof) / ``live_run``
(live orchestration). This file is only the CLI entrypoint.

Paths are repository-relative (media renders under .scratch, removed after
the run). Run from video-pipeline/: uv run python <this file>. Use
``--selfcheck`` for the non-live stale/timeout render proof (no Resolve).
"""

from __future__ import annotations

import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
sys.path.insert(0, str(VIDEO_PIPELINE))
sys.path.insert(0, str(VIDEO_PIPELINE / "tests" / "mcp_client"))
sys.path.insert(0, str(EVIDENCE))


def main() -> int:
    if "--selfcheck" in sys.argv:
        from render_freshness import selfcheck  # noqa: PLC0415

        return selfcheck()
    from live_run import run_live  # noqa: PLC0415

    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
