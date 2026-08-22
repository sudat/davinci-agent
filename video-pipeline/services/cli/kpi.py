"""``python -m services.cli.kpi`` — Phase-9 KPI aggregation (task 58).

Subcommands:
  evaluate — episode run reports (models' JSON paths) → kpi-report-v1 JSON
  summary  — kpi-report-v1 JSON → Japanese human-readable summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.kpi_evaluation import (
    KpiEvaluationError,
    KpiReportV1,
    evaluate_kpis,
    render_kpi_summary,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.kpi",
        description="Phase-9 KPI aggregation over episode run reports.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser(
        "evaluate", help="aggregate episode run reports into kpi-report-v1"
    )
    evaluate.add_argument(
        "--report",
        type=Path,
        nargs="+",
        required=True,
        help="episode run report JSON paths (episode-run-report input shape)",
    )
    evaluate.add_argument(
        "--out", type=Path, default=None, help="write kpi-report-v1 JSON to this path"
    )

    summary = sub.add_parser("summary", help="render a Japanese summary to stdout")
    summary.add_argument("--report", type=Path, required=True, help="kpi-report-v1 JSON")

    return parser


def _cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        report = evaluate_kpis(args.report)
        payload = canonical_model_bytes(report)
    except (KpiEvaluationError, ValidationError, OSError) as exc:
        print(f"kpi_failed: {exc}", file=sys.stderr)
        return 1
    if args.out is not None:
        atomic_write(args.out, payload)
        print(
            f"kpi: {args.out} episodes={report.episode_count} "
            f"insufficient_data={report.insufficient_data}"
        )
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    try:
        report = KpiReportV1.model_validate(
            json.loads(args.report.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"kpi_failed: {exc}", file=sys.stderr)
        return 1
    print(render_kpi_summary(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "evaluate":
        return _cmd_evaluate(args)
    return _cmd_summary(args)


if __name__ == "__main__":
    raise SystemExit(main())
