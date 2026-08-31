# noqa: INP001 (evidence tree is not an importable package by design)
"""CLI for the resolve auto-caption disposable capability probe (Task 1).

Usage (from ``video-pipeline/``), with ``PROBE`` shorthand for
``capabilities/v4.4/probes/resolve-auto-caption/probe_autocaption.py``::

  PYTHONPATH=. uv run python $PROBE --selfcheck
  PYTHONPATH=. uv run python $PROBE --run r1|r2      # live; guarded, once
  PYTHONPATH=. uv run python $PROBE --stability
  PYTHONPATH=. uv run python $PROBE --reconstruct r1|r2

``--reconstruct`` rebuilds the sanitized capability JSON purely from the
existing private ``steps.json``/``readback.json`` bytes (field-name
corrections, recorded timeline start, relative spans) — never a new live
run; every number traces to those recorded responses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
REPO = VIDEO_PIPELINE.parent
for _path in (VIDEO_PIPELINE, EVIDENCE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import caption_logic as logic  # noqa: E402 (path bootstrap first — task4 pattern)
from probe_live import (  # noqa: E402 (path bootstrap first — task4 pattern)
    PRIVATE_RUNS_DIR,
    RUN_LABELS,
    live_run,
)
from probe_selfcheck import flow_selfcheck  # noqa: E402 (path bootstrap first)


def print_report(report: dict[str, object]) -> None:
    """Counts / hashes / labels / versions only — never caption prose."""
    print(f"run={report.get('run')} verdict={report.get('verdict')}", flush=True)
    for key in ("resolve_version", "mcp_version", "pin_commit",
                "timeline_start_frame"):
        print(f"{key}={report.get(key)}", flush=True)
    print(f"input_media_sha256={report.get('input_media_sha256')} "
          f"bytes={report.get('input_media_bytes')}", flush=True)
    for key in ("generation", "readback", "audio_placement", "cleanup"):
        block = report.get(key)
        if isinstance(block, dict):
            print(f"{key}={json.dumps(block, sort_keys=True, ensure_ascii=False)}",
                  flush=True)
    candidates_block = report.get("settings_candidates", [])
    if isinstance(candidates_block, list):
        for candidate in candidates_block:
            if isinstance(candidate, dict):
                print(f"candidate value={candidate.get('value')} "
                      f"accepted={candidate.get('accepted')} "
                      f"ignored_keys={candidate.get('ignored_keys')} "
                      f"echo_setting_count={candidate.get('echo_setting_count')}",
                      flush=True)
    failure = report.get("failure", {})
    if isinstance(failure, dict) and failure:
        print(f"failure reason={failure.get('reason')} phase={failure.get('phase')}",
              flush=True)
    print(f"elapsed_ms={report.get('elapsed_ms')}", flush=True)


def _steps_response(steps: list[dict[str, object]], tool: str, action: str,
                    *, last: bool = True) -> dict[str, object]:
    """One recorded raw response from private steps.json (typed extraction)."""
    matches = [row for row in steps
               if row.get("tool") == tool and row.get("action") == action
               and isinstance(row.get("response"), dict)]
    if not matches:
        raise logic.ProbeRefusalError("error", f"steps missing {tool}.{action}")
    chosen = matches[-1] if last else matches[0]
    response = chosen.get("response")
    if not isinstance(response, dict):
        raise logic.ProbeRefusalError("error", "recorded response not a dict")
    return response


def reconstruct(run_label: str) -> int:
    """Rebuild sanitized capability JSON from the existing private bytes."""
    run_dir = PRIVATE_RUNS_DIR / f"resolve-auto-caption-{run_label}"
    steps = json.loads((run_dir / "steps.json").read_text())
    if not isinstance(steps, list):
        raise logic.ProbeRefusalError("error", "steps not a list")
    raw = json.loads((run_dir / "readback.json").read_text())
    rows = raw.get("cues")
    cues, canonical_sha = logic.canonical_cues(rows if isinstance(rows, list) else [])
    current = _steps_response(steps, "timeline", "get_current")
    start = current.get("start_frame")
    if not isinstance(start, int):
        raise logic.ProbeRefusalError("error", "timeline start not recorded")
    count_before = _steps_response(steps, "timeline", "get_track_count").get("count")
    relative = [{"text": text, "start_relative": c_start - start,
                 "end_relative": c_end - start}
                for text, c_start, c_end in cues]
    (run_dir / "readback-context.json").write_text(json.dumps({
        "timeline_start_frame": start,
        "absolute_cue_count": len(cues),
        "canonical_sha256_absolute": canonical_sha,
        "note": "relative frames = absolute record frame - timeline_start_frame",
        "relative_cues": relative,
    }, ensure_ascii=False, indent=1) + "\n")
    report = json.loads((EVIDENCE / f"capability-{run_label}.json").read_text())
    readback = report.get("readback")
    if not isinstance(readback, dict):
        raise logic.ProbeRefusalError("error", "report readback block missing")
    report["timeline_start_frame"] = start
    readback.update({
        "item_count": len(cues),
        "min_start_frame": min((c[1] for c in cues), default=0),
        "max_end_frame": max((c[2] for c in cues), default=0),
        "relative_min_start_frame": min((c[1] for c in cues), default=0) - start,
        "relative_max_end_frame": max((c[2] for c in cues), default=0) - start,
        "canonical_sha256": canonical_sha,
    })
    placement = report.get("audio_placement")
    if isinstance(placement, dict) and isinstance(count_before, int):
        placement["audio_track_count_before"] = count_before
        placement.pop("audio_track_items_before", None)
    logic.assert_sanitized(report)
    (EVIDENCE / f"capability-{run_label}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(f"reconstructed run={run_label} timeline_start_frame={start} "
          f"items={len(cues)} canonical_sha256={canonical_sha} "
          f"source=private-steps-readback", flush=True)
    return 0


def stability() -> int:
    """Compare r1/r2 private readbacks; print the overall capability verdict."""
    verdicts: dict[str, str] = {}
    cues_by_run: dict[str, list[logic.Cue]] = {}
    sha_by_run: dict[str, str] = {}
    for run_label in RUN_LABELS:
        capability = EVIDENCE / f"capability-{run_label}.json"
        readback = (PRIVATE_RUNS_DIR / f"resolve-auto-caption-{run_label}"
                    / "readback.json")
        if not capability.is_file() or not readback.is_file():
            print(f"capability=capability-unproven missing=run-{run_label}", flush=True)
            return 1
        verdicts[run_label] = str(
            json.loads(capability.read_text()).get("verdict", "error"))
        rows = json.loads(readback.read_text()).get("cues")
        if not isinstance(rows, list):
            print(f"capability=capability-unproven invalid=readback-{run_label}",
                  flush=True)
            return 1
        try:
            cues, sha = logic.canonical_cues(rows)
        except logic.ProbeRefusalError:
            print(f"capability=capability-unproven invalid=readback-{run_label}",
                  flush=True)
            return 1
        cues_by_run[run_label] = cues
        sha_by_run[run_label] = sha
    compared = logic.stability_verdict(
        cues_by_run["r1"], sha_by_run["r1"], cues_by_run["r2"], sha_by_run["r2"])
    receipts = {run_label: json.loads(
        (EVIDENCE / f"capability-{run_label}.json").read_text()).get("cleanup", {})
        for run_label in RUN_LABELS}
    clean = all(isinstance(receipts[r], dict)
                and receipts[r].get("delete_success") is True
                and receipts[r].get("load_after_delete_ok") is False
                for r in RUN_LABELS)
    proven = bool(all(verdicts[r] == "generated-and-read" for r in RUN_LABELS)
                  and compared["stable"] is True and clean)
    print(f"capability={'capability-proven' if proven else 'capability-unproven'}",
          flush=True)
    print(f"stability stable={compared['stable']} "
          f"r1_sha256={compared['r1_sha256']} r2_sha256={compared['r2_sha256']} "
          f"r1_items={compared['r1_item_count']} r2_items={compared['r2_item_count']}",
          flush=True)
    print(f"cleanup r1={receipts['r1']} r2={receipts['r2']}", flush=True)
    return 0 if proven else 1


def run_guarded(run_label: str) -> int:
    capability = EVIDENCE / f"capability-{run_label}.json"
    run_dir = PRIVATE_RUNS_DIR / f"resolve-auto-caption-{run_label}"
    if capability.exists() or (run_dir / "readback.json").exists():
        print(f"verdict=run-already-recorded run={run_label}", flush=True)
        return 1
    report, exit_code = live_run(run_label)
    capability.write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print_report(report)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--selfcheck", action="store_true",
                        help="synthetic-data verification only (no Resolve)")
    parser.add_argument("--run", choices=RUN_LABELS,
                        help="one disposable live session (exactly r1 or r2)")
    parser.add_argument("--reconstruct", choices=RUN_LABELS,
                        help="rebuild sanitized evidence from private bytes only")
    parser.add_argument("--stability", action="store_true",
                        help="compare r1/r2 readbacks and print the verdict")
    args = parser.parse_args()
    if args.selfcheck:
        return flow_selfcheck()
    if args.run is not None:
        return run_guarded(args.run)
    if args.reconstruct is not None:
        return reconstruct(args.reconstruct)
    if args.stability:
        return stability()
    parser.print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
