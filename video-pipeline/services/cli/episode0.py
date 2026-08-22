"""``python -m services.cli.episode0`` — Episode-0 baseline tooling.

Subcommands:
  freeze-manifest — hash/size/duration for real-01 source (from episode.json)
  report          — operator log + manifest → runs/<run-id>/report.json
  compare         — delta summary between two report.json files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.episode0_baseline import (
    Episode0BaselineLogV1,
    Episode0SourceManifest,
    compare_reports,
    compute_manifest_for_episode_json,
    generate_report,
)

DEFAULT_EPISODE_JSON = Path("private/reference-episodes/real-01/episode.json")
DEFAULT_RUNS_ROOT = Path("private/reference-episodes/real-01/runs")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.episode0",
        description="Episode-0 longitudinal baseline tooling.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze-manifest", help="freeze source manifest from episode.json")
    freeze.add_argument("--episode-json", type=Path, default=DEFAULT_EPISODE_JSON)
    freeze.add_argument("--out", type=Path, default=None, help="write manifest json to this path")

    report = sub.add_parser("report", help="operator log + manifest → report.json")
    report.add_argument("--log", type=Path, required=True, help="path to episode0-baseline-v1 json")
    report.add_argument("--run-id", type=str, required=True, help="run id (baseline or ISO date)")
    report.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="precomputed manifest json; if omitted, derived from --episode-json",
    )
    report.add_argument("--episode-json", type=Path, default=DEFAULT_EPISODE_JSON)
    report.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)

    compare = sub.add_parser("compare", help="delta summary between two reports")
    compare.add_argument("--a", type=Path, required=True, help="first report.json")
    compare.add_argument("--b", type=Path, required=True, help="second report.json")
    compare.add_argument("--out", type=Path, default=None, help="write delta json to this path")

    return parser


def _cmd_freeze_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = compute_manifest_for_episode_json(args.episode_json)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"freeze_failed: {exc}", file=sys.stderr)
        return 1
    # Also handle direct video path if episode.json missing? Already handled.
    payload = canonical_model_bytes(manifest)
    if args.out is not None:
        atomic_write(args.out, payload)
        print(f"manifest: {args.out} sha256={manifest.sha256[:12]} size={manifest.size_bytes}")
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        log_data: object = json.loads(args.log.read_text(encoding="utf-8"))
        log = Episode0BaselineLogV1.model_validate(log_data)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"log_unreadable: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"log_invalid: {exc}", file=sys.stderr)
        return 1

    try:
        if args.manifest is not None:
            raw: object = json.loads(args.manifest.read_text(encoding="utf-8"))
            manifest = Episode0SourceManifest.model_validate(raw)
        else:
            manifest = compute_manifest_for_episode_json(args.episode_json)
    except (OSError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        print(f"manifest_unreadable: {exc}", file=sys.stderr)
        return 1

    try:
        out = generate_report(log, manifest, run_id=args.run_id, runs_root=args.runs_root)
    except (ValueError, OSError, ValidationError) as exc:
        print(f"report_failed: {exc}", file=sys.stderr)
        return 1
    print(f"report: {out} run_id={args.run_id}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        delta = compare_reports(args.a, args.b)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(f"compare_failed: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(
        delta, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode() + b"\n"
    if args.out is not None:
        atomic_write(args.out, payload)
        print(f"delta: {args.out}")
    else:
        sys.stdout.buffer.write(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "freeze-manifest":
        return _cmd_freeze_manifest(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "compare":
        return _cmd_compare(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
