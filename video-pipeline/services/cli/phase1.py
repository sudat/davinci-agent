"""``python -m services.cli.phase1`` — the deterministic Phase-1 fixture run.

``run --episode-root <dir> --stop <STAGE> --out <dir>`` executes the frozen
chain (eligibility -> analyzers -> director replay -> selection commit ->
planner -> edit plan commit -> production IR -> review-plane projection ->
review store -> preview) for a fixture episode and stops at the requested
stage; at PREVIEW_READY it emits the review bundle the H1 tooling consumes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.cli.chain import STAGE_ORDER, ChainError
from services.cli.run_stage import run_phase1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.phase1",
        description="Run the deterministic Phase-1 fixture chain up to a stage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the chain for one fixture episode")
    run.add_argument("--episode-root", type=Path, required=True)
    run.add_argument("--stop", choices=STAGE_ORDER, required=True)
    run.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        outcome = run_phase1(
            arguments.episode_root, arguments.stop, arguments.out
        )
    except ChainError as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 1
    report = outcome.report
    print(f"episode: {report.episode_id}")
    print(f"stage: {report.stop_stage}")
    print(f"edit-source world: {report.edit_source_world_sha256[:12]}")
    if report.bundle_path:
        print(f"review bundle: {report.bundle_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
