# noqa: INP001 (evidence tree is not an importable package by design)
"""Live orchestration for the Task 4 final probe (loaded only for live runs).

Owns the evidence-freshness reset (render tree, MCP call ledger), the
ordered probe sequence, disposable-project cleanup, and the summary/
scrub/rmtree tail. Importing this module loads the live client stack.
"""

from __future__ import annotations

import contextlib
import json
import shutil

import live_support
from evidence_io import (
    EVIDENCE,
    LEDGER_DIR,
    RENDER_DIR,
    SCRATCH,
    reset_dir,
    scrub_repo_prefixes,
)
from probe_session import BOUND_FONT, CUES, ProbeState, open_session
from probe_steps import (
    probe_a_autocaption,
    probe_b2_rerun,
    probe_b_one_cue,
    probe_c_all_cues,
    probe_p1_session,
    probe_p2_prepare_product_adapter,
    probe_prod_independent,
)
from render_evidence import probe_d_render

_PROBES = [
    ("p1-session", probe_p1_session),
    ("p2-prepare-product-adapter", probe_p2_prepare_product_adapter),
    ("a-autocaption-expressibility", probe_a_autocaption),
    ("b-one-cue-native", probe_b_one_cue),
    ("b2-rerun-identity", probe_b2_rerun),
    ("c-all-cues", probe_c_all_cues),
    ("d-render-evidence", probe_d_render),
    ("prod-independent-readback", probe_prod_independent),
]


def run_live() -> int:
    # Evidence freshness: render outputs and the MCP call ledger are
    # regenerated for THIS run only — a stale file or appended old calls
    # can never satisfy a new probe pass.
    reset_dir(RENDER_DIR)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    live_support.PROBES_DIR = EVIDENCE
    live_support.LEDGER_DIR = LEDGER_DIR
    reset_dir(LEDGER_DIR)
    session = open_session()
    state: ProbeState = {}
    summary: list[dict[str, object]] = []
    try:
        for capability, fn in _PROBES:
            log = live_support.run_probe(session, capability, fn, state)
            summary.append(
                {k: log[k] for k in ("capability", "status", "readback", "wall_seconds")}
            )
            print(f"[{log['status']}] {capability}: {str(log['readback'])[:260]}", flush=True)
    finally:
        if "project_name" in state:
            session.project_name = state["project_name"]
        with contextlib.suppress(Exception):
            live_support.close_live_session(session, delete_project=True)
    failed = [row for row in summary if row["status"] != "ok"]
    (EVIDENCE / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "task4-subtitle-probe-v3",
                "run": (
                    "style-completeness repair (full Font/Size/Center readback "
                    "compare + fresh-evidence render/ledger reset)"
                ),
                "resolve_version": session.resolve_version_string,
                "bound_font": BOUND_FONT,
                "cues": [dict(c) for c in CUES],
                "probes": summary,
                "evidence_files": [
                    "font-diagnosis.json",
                    "font-render-arms.json",
                    "rendered-span-proof.json",
                    "product-run.json",
                    "ledger/",
                    "render/",
                ],
            },
            indent=1,
            ensure_ascii=False,
            default=str,
        )
        + "\n"
    )
    scrub_repo_prefixes()
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"DONE failed={len(failed)} evidence={EVIDENCE}", flush=True)
    return 1 if failed else 0


__all__ = ["run_live"]
