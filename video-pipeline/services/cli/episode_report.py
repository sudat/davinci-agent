"""``python -m services.cli.episode_report`` — Phase-6 measurement harness CLI.

Subcommands (plan task 52):
  collect        — episode root → episode-run-report-v1 (auto + operator fields)
  aggregate      — three report files → phase6-summary-v1
  check-footage  — pre-start Episode A/B/C footage decision record
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.episode_run_report import (
    EpisodeRunReportError,
    EpisodeRunReportV1,
    FootageDecisionV1,
    FootageOperatorInputs,
    OperatorEpisodeMeasurements,
    Phase6SummaryV1,
    aggregate_three_episodes,
    check_footage_sources,
    collect_episode_run,
)

EPISODE_CLASSES = ("a_talking_broll", "b_visual_first", "c_mixed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.episode_report",
        description="Episode A/B/C measurement harness (Phase 6 / Gate V43-4b).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="episode root → episode-run-report-v1")
    collect.add_argument("--episode-root", type=Path, required=True)
    collect.add_argument("--class", dest="episode_class", required=True, choices=EPISODE_CLASSES)
    collect.add_argument(
        "--operator-json",
        type=Path,
        default=None,
        help="optional OperatorEpisodeMeasurements overlay json",
    )
    collect.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default <episode-root>/episode-run-report.json)",
    )

    aggregate = sub.add_parser("aggregate", help="report files → phase6-summary-v1")
    aggregate.add_argument("--report", dest="reports", type=Path, required=True, action="append")
    aggregate.add_argument("--out", type=Path, default=None)

    footage = sub.add_parser("check-footage", help="pre-start footage decision record")
    footage.add_argument(
        "--real01-episode-json",
        type=str,
        default="private/reference-episodes/real-01/episode.json",
    )
    footage.add_argument("--source-a", dest="sources_a", type=str, action="append", default=[])
    footage.add_argument("--source-c", dest="sources_c", type=str, action="append", default=[])
    footage.add_argument("--out", type=Path, default=None)
    return parser


def _cmd_collect(args: argparse.Namespace) -> int:
    measurements: OperatorEpisodeMeasurements | None = None
    if args.operator_json is not None:
        try:
            raw: object = json.loads(args.operator_json.read_text(encoding="utf-8"))
            measurements = OperatorEpisodeMeasurements.model_validate(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"operator_json_unreadable: {exc}", file=sys.stderr)
            return 1
        except ValidationError as exc:
            print(f"operator_json_invalid: {exc}", file=sys.stderr)
            return 1
    try:
        report = collect_episode_run(
            args.episode_root,
            episode_class=args.episode_class,
            operator_measurements=measurements,
        )
    except EpisodeRunReportError as exc:
        print(f"collect_failed: {exc}", file=sys.stderr)
        return 1
    out = args.out or args.episode_root / "episode-run-report.json"
    try:
        atomic_write(out, canonical_model_bytes(report))
    except OSError as exc:
        print(f"write_failed: {exc}", file=sys.stderr)
        return 1
    print(f"report: {out} coverage={report.field_coverage.filled}/{report.field_coverage.total}")
    return 0


def _cmd_aggregate(args: argparse.Namespace) -> int:
    reports: list[EpisodeRunReportV1] = []
    for path in args.reports:
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
            reports.append(EpisodeRunReportV1.model_validate(payload))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"report_unreadable: {path}: {exc}", file=sys.stderr)
            return 1
        except ValidationError as exc:
            print(f"report_invalid: {path}: {exc}", file=sys.stderr)
            return 1
    try:
        summary = aggregate_three_episodes(reports)
    except EpisodeRunReportError as exc:
        print(f"aggregate_failed: {exc}", file=sys.stderr)
        return 1
    payload = canonical_model_bytes(summary)
    if args.out is not None:
        try:
            atomic_write(args.out, payload)
        except OSError as exc:
            print(f"write_failed: {exc}", file=sys.stderr)
            return 1
        print(f"summary: {args.out}")
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    return 0


def _cmd_check_footage(args: argparse.Namespace) -> int:
    decision = check_footage_sources(
        FootageOperatorInputs(
            real01_episode_json=args.real01_episode_json,
            episode_a_sources=tuple(args.sources_a),
            episode_c_sources=tuple(args.sources_c),
        )
    )
    payload = canonical_model_bytes(decision)
    if args.out is not None:
        try:
            atomic_write(args.out, payload)
        except OSError as exc:
            print(f"write_failed: {exc}", file=sys.stderr)
            return 1
        print(f"decision: {args.out}")
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "collect":
        return _cmd_collect(args)
    if args.command == "aggregate":
        return _cmd_aggregate(args)
    return _cmd_check_footage(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FootageDecisionV1", "Phase6SummaryV1", "main"]
