"""``python -m services.cli.legacy_report`` — legacy removal decision report.

Renders the machine-judged removal-decision table (Japanese headers) and
optionally writes the canonical ``legacy-removal-report-v1`` JSON. The
report never deletes anything; deletion is a separate operator-approved
task (task 59 scope: report only).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel, to_tuple
from services.foundation_io import atomic_write, canonical_model_bytes
from services.release.legacy_removal import (
    ConformanceGuardEntry,
    LedgerDirSource,
    LegacyRemovalError,
    LegacyRemovalReportV1,
    RunReportSource,
    evaluate_legacy_removal,
)
from services.toolchain.mcp_fit import McpFitError, load_mcp_fit


class ConformanceGuardRegistryV1(StrictModel):
    """Registry file contract mapping guards to candidates/capabilities."""

    schema_version: Literal["conformance-guard-registry-v1"]
    guards: Annotated[
        tuple[ConformanceGuardEntry, ...], BeforeValidator(to_tuple)
    ] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _guard_ids_unique(self) -> ConformanceGuardRegistryV1:
        ids = [guard.guard_id for guard in self.guards]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_guard_id", "guard_id values must be unique"
            )
        return self

DEFAULT_MCP_FIT: Final = Path("capabilities/v4.3/mcp-fit.json")
DEFAULT_LEDGER_DIR: Final = Path("capabilities/v4.3/runs/probes/ledger")

_TABLE_HEADER: Final = (
    "候補ID | MCP実使用(≥3) | フォールバック不要 | 固有conformanceガードなし | 推奨"
)
_TABLE_NOTE: Final = (
    "注: unknown は証拠不足のため削除推奨しない。削除実行はOperator承認後の別作業。"
)


def render_report_table(report: LegacyRemovalReportV1) -> str:
    """Japanese-header decision table: one row per candidate, three conditions."""
    lines = [
        f"Legacy削除判定レポート ({report.schema_version})",
        _TABLE_HEADER,
    ]
    for candidate in report.candidates:
        use, fallback, guards = candidate.conditions
        cells = (
            f"{use.verdict}({candidate.mcp_production_use_count}回)",
            fallback.verdict,
            guards.verdict,
            candidate.recommendation,
        )
        lines.append(f"{candidate.candidate_id} | {' | '.join(cells)}")
    lines.append(_TABLE_NOTE)
    return "\n".join(lines)


def _registry_entries(path: Path | None) -> tuple[ConformanceGuardEntry, ...]:
    if path is None:
        return ()
    registry = ConformanceGuardRegistryV1.model_validate(
        json.loads(path.read_bytes().decode("utf-8"))
    )
    return registry.guards


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.legacy_report",
        description="Legacy removal decision report (report only; no deletion).",
    )
    parser.add_argument("--mcp-fit", type=Path, default=DEFAULT_MCP_FIT)
    parser.add_argument("--ledger-dir", type=Path, action="append", default=[])
    parser.add_argument("--run-report", type=Path, action="append", default=[])
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    ledger_dirs = arguments.ledger_dir or [DEFAULT_LEDGER_DIR]
    ledger_sources = tuple(
        LedgerDirSource(kind="ledger_dir", path=str(item)) for item in ledger_dirs
    )
    run_sources = tuple(
        RunReportSource(kind="run_report", path=str(item))
        for item in arguments.run_report
    )
    try:
        report = evaluate_legacy_removal(
            mcp_fit=load_mcp_fit(arguments.mcp_fit),
            call_ledgers=(*ledger_sources, *run_sources),
            conformance_registry=_registry_entries(arguments.registry),
        )
    except (LegacyRemovalError, McpFitError, OSError, ValueError) as error:
        print(f"legacy_report_failed: {error}", file=sys.stderr)
        return 2
    print(render_report_table(report))
    if arguments.out is not None:
        atomic_write(arguments.out, canonical_model_bytes(report))
        print(f"report: {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "render_report_table"]
