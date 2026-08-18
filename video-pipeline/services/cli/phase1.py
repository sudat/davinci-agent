"""``python -m services.cli.phase1`` — the Phase-1 episode run (Todo 46).

``run --episode-root <dir> --stop <STAGE> --out <dir>`` branches on the
episode root: a parseable frozen ``manifest.json`` runs the EXISTING fixture
chain unchanged; an ``episode.json`` runs the REAL-episode chain (real
ingest/normalize/analyzers, director honestly labeled per mode) to
PREVIEW_READY; anything else is a clear typed error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from services.cli.chain import STAGE_ORDER, ChainError
from services.cli.real_chain import RealChainError, RealChainReport, run_real_chain
from services.cli.run_stage import run_phase1
from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

FIXTURE_MANIFEST = "manifest.json"
REAL_MANIFEST = "episode.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.phase1",
        description="Run the deterministic Phase-1 chain up to a stage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the chain for one episode")
    run.add_argument("--episode-root", type=Path, required=True)
    run.add_argument("--stop", choices=STAGE_ORDER, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="resolved production policy snapshot for the real path (live director)",
    )
    return parser


def _is_fixture_root(episode_root: Path) -> bool:
    path = episode_root / FIXTURE_MANIFEST
    if not path.is_file():
        return False
    Phase1TechnicalFixtureManifest.model_validate_json(path.read_bytes())
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if _is_fixture_root(arguments.episode_root):
            outcome = run_phase1(arguments.episode_root, arguments.stop, arguments.out)
            report = outcome.report
        elif (arguments.episode_root / REAL_MANIFEST).is_file():
            real = run_real_chain(
                arguments.episode_root,
                arguments.stop,
                arguments.out,
                policy_path=arguments.policy,
            )
            report = real.report
        else:
            print(
                f"episode_root_invalid: {arguments.episode_root} carries neither a "
                f"parseable fixture {FIXTURE_MANIFEST} nor a real {REAL_MANIFEST}",
                file=sys.stderr,
            )
            return 1
    except (ChainError, RealChainError) as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 1
    except ValidationError as error:
        print(f"fixture_manifest_invalid: {error}", file=sys.stderr)
        return 1
    print(f"episode: {report.episode_id}")
    print(f"stage: {report.stop_stage}")
    print(f"edit-source world: {report.edit_source_world_sha256[:12]}")
    if isinstance(report, RealChainReport) and report.director_mode:
        print(f"director mode: {report.director_mode}")
        print(f"director served by: {report.director_served_by}")
    if report.bundle_path:
        print(f"review bundle: {report.bundle_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
