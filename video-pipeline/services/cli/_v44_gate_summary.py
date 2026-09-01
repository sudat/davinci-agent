"""V44-2 gate-summary derivation for the T13 finishing CLI (T16).

``build_v44_2_summary`` DERIVES every field from the episode workspace —
``finishing/finishing-run.json`` (blocked domains, QC verdicts, pins),
``review/publishability.json`` (operator verdict), ``time-log.jsonl``
(bootstrap AHT total + direct-Resolve minutes; a phase with zero recorded
lines stays ``None``, never a fabricated 0). It refuses (typed) when the
finishing report or the publishability record is missing — nothing to
summarize. ``passed`` is computed, never asserted: the model validator in
``services.metrics.v44_gate_state`` is the structural backstop.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.cli._v44_finishing_record import (
    FINISHING_REPORT_RELATIVE,
    PUBLISHABILITY_RELATIVE,
    TIME_LOG_NAME,
)
from services.cli._v44_finishing_report import (
    FinishingRunReportV1,
    TimeLogLineV1,
    summarize_time_log,
)
from services.cli._v44_publishability_binding import load_bound_publishability
from services.metrics.v44_gate_state import V44GateSummaryV1, write_v44_2_summary

GATE_SUMMARY_NAME: Final = "gate-summary.json"
EXIT_PASSED: Final = 0
EXIT_BLOCKED: Final = 1

#: PublishabilityReviewV1.publishable -> the summary's operator verdict.
VERDICT_FROM_REVIEW: Final[dict[str, str]] = {
    "as_is": "publishable",
    "after_small_corrections": "publishable_after_fixes",
    "not_yet": "not_publishable",
}

#: Evidence files recorded in ``artifacts`` when present (runbook §1.5 order).
_ARTIFACT_CANDIDATES: Final[tuple[tuple[str, ...], ...]] = (
    FINISHING_REPORT_RELATIVE,
    ("finishing", "quality-domain-report.json"),
    ("finishing", "editorial-qc-report.json"),
    ("finishing", "qc-report.json"),
    ("finishing", "mcp-run-report.json"),
    PUBLISHABILITY_RELATIVE,
    (TIME_LOG_NAME,),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json[T: BaseModel](
    path: Path, model: Callable[[bytes], T], schema_note: str
) -> T:
    try:
        return model(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FinishingMalformedError(
            "evidence-invalid", f"{path} is not {schema_note}: {error}"
        ) from error


def _sum_time_log(path: Path) -> tuple[float | None, float | None]:
    """(bootstrap AHT total, direct-Resolve minutes); None = not recorded."""

    if not path.is_file():
        return None, None
    lines: list[TimeLogLineV1] = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(TimeLogLineV1.model_validate_json(raw))
        except ValidationError as error:
            raise FinishingMalformedError(
                "time-log-invalid", f"{path} has a non-v44-time-log-v1 line: {error}"
            ) from error
    totals = summarize_time_log(lines)
    return totals.aht_minutes, totals.direct_resolve_minutes


def build_v44_2_summary(
    episode_root: Path, *, commit_sha: str, subtitle_proof_ref: str | None = None
) -> V44GateSummaryV1:
    """Derive the gate summary from run evidence; typed refusal when the
    finishing report or the publishability record is absent."""

    report_path = episode_root.joinpath(*FINISHING_REPORT_RELATIVE)
    if not report_path.is_file():
        raise FinishingError(
            "finishing-report-missing",
            f"no finishing report at {report_path}; run "
            f"`python -m services.cli.v44_finishing run` first",
        )
    report = _load_json(
        report_path,
        FinishingRunReportV1.model_validate_json,
        "a v44-finishing-run-v1 report",
    )

    review = load_bound_publishability(episode_root, report=report)

    aht_minutes, direct_minutes = _sum_time_log(episode_root / TIME_LOG_NAME)
    verdict = VERDICT_FROM_REVIEW[review.publishable]
    passed = (
        verdict in ("publishable", "publishable_after_fixes")
        and not report.blocked_domains
        and report.technical_qc.verdict == "passed"
        and report.editorial_qc.critical_count == 0
        and aht_minutes is not None
        and direct_minutes is not None
    )
    return V44GateSummaryV1.model_validate(
        {
            "passed": passed,
            "operator_verdict": verdict,
            "blocked_domains": report.blocked_domains,
            "technical_qc": report.technical_qc.verdict,
            "editorial_qc_blocked_items": report.editorial_qc.critical_count,
            "bootstrap_aht_minutes": aht_minutes,
            "direct_resolve_minutes": direct_minutes,
            "director_pin_model": report.director_model_id,
            "evidence_pins": {"analysis_provider": report.analysis_provider},
            "finishing_report_ref": str(Path(*FINISHING_REPORT_RELATIVE)),
            "subtitle_proof_ref": subtitle_proof_ref,
            "artifacts": [
                str(Path(*relative))
                for relative in _ARTIFACT_CANDIDATES
                if episode_root.joinpath(*relative).is_file()
            ],
            "recorded_at": _now_iso(),
            "commit_sha": commit_sha,
        }
    )


def _head_commit_sha(cwd: Path) -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise FinishingMalformedError(
            "commit-sha-unavailable",
            f"git rev-parse HEAD failed in {cwd}: {result.stderr.strip()}",
        )
    return result.stdout.strip()


def _not_passed_reasons(summary: V44GateSummaryV1) -> tuple[str, ...]:
    reasons: list[str] = []
    if summary.operator_verdict is None:
        reasons.append("operator verdict missing")
    elif summary.operator_verdict == "not_publishable":
        reasons.append("operator verdict is not_publishable")
    if summary.blocked_domains:
        reasons.append(f"blocked domains: {', '.join(summary.blocked_domains)}")
    if summary.technical_qc != "passed":
        reasons.append(f"technical QC: {summary.technical_qc}")
    if summary.editorial_qc_blocked_items:
        reasons.append(f"editorial QC blocked items: {summary.editorial_qc_blocked_items}")
    if summary.bootstrap_aht_minutes is None:
        reasons.append("bootstrap AHT minutes not recorded (record-time)")
    if summary.direct_resolve_minutes is None:
        reasons.append(
            "direct-Resolve minutes not recorded (record-time --phase direct_resolve)"
        )
    return tuple(reasons)


def cmd_gate_summary(args: argparse.Namespace) -> int:
    episode_root: Path = args.episode_root
    if not episode_root.is_dir():
        raise FinishingError(
            "episode-root-not-found", f"{episode_root} is not a directory"
        )
    commit_sha: str = (
        args.commit_sha
        if args.commit_sha is not None
        else _head_commit_sha(Path(__file__).resolve().parents[2])
    )
    try:
        summary = build_v44_2_summary(
            episode_root,
            commit_sha=commit_sha,
            subtitle_proof_ref=args.subtitle_proof_ref,
        )
    except ValidationError as error:
        raise FinishingMalformedError("summary-invalid", str(error)) from error
    out: Path = args.out if args.out is not None else episode_root / "finishing" / GATE_SUMMARY_NAME
    write_v44_2_summary(out, summary)
    print(f"gate summary: {out}")
    print(f"gate: V44-2 passed: {summary.passed}")
    if summary.passed:
        return EXIT_PASSED
    for reason in _not_passed_reasons(summary):
        print(f"not passed: {reason}", file=sys.stderr)
    return EXIT_BLOCKED


__all__ = [
    "GATE_SUMMARY_NAME",
    "build_v44_2_summary",
    "cmd_gate_summary",
]
