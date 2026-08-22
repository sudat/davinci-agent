"""Technical-QC aggregation over PROVIDER EVIDENCE (task 39; PRD 14.1).

Generic technical checks (gaps/overlaps, missing media, loudness,
conform, blanking, render format, timeline readback, delivery manifest)
prefer the MCP/advanced servers. This module NEVER re-implements a
detector: it aggregates typed :class:`ProviderCheckV1` rows — each
carrying an evidence reference (path/URI/id) into the provider's own
report — plus readback-derived rows where the run report itself IS the
evidence (verified placements = timeline readback / conform evidence).
The summary maps onto the existing QC report conventions
(:class:`~services.qc.models.QcIssue`-shaped rows) so the project QC
report can point at provider evidence instead of duplicating it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import StrictModel

TechnicalCheckId = Literal[
    "gaps_overlaps",
    "missing_media",
    "conform_source_ranges",
    "loudness",
    "blanking_black_frames",
    "render_format_codec",
    "timeline_readback",
    "delivery_manifest",
]

CheckStatus = Literal["pass", "fail", "not_available"]
ProviderId = Literal["mcp", "advanced"]

#: Technical check -> existing QcRuleId where a natural rule exists.
#: Unmapped checks stay in the summary (counts + verdict) without a row —
#: fabricating a rule binding would re-implement the detector's meaning.
_QC_RULE: Final[dict[TechnicalCheckId, str]] = {
    "gaps_overlaps": "ir_span_gap",
    "missing_media": "ir_binding_missing",
    "conform_source_ranges": "ir_totals_mismatch",
    "loudness": "audio_loudness_out_of_range",
    "blanking_black_frames": "video_black_span",
    "render_format_codec": "video_metadata_mismatch",
}

_PLACEMENT_ACTIONS: Final[frozenset[str]] = frozenset(
    {"place_clip", "place_overlay", "place_title", "place_audio"}
)


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class ProviderCheckV1(StrictModel):
    """One provider-side technical check result + its evidence reference."""

    check: TechnicalCheckId
    provider: ProviderId
    status: CheckStatus
    evidence_ref: str = Field(min_length=1, strict=True)
    detail: str = ""


class TechnicalQcSummaryV1(StrictModel):
    """Aggregated technical-QC summary (provider evidence referenced)."""

    schema_version: Literal["technical-qc-summary-v1"]
    checks: Annotated[tuple[ProviderCheckV1, ...], BeforeValidator(_to_tuple)] = ()
    passed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    not_available: int = Field(ge=0, strict=True)
    verdict: Literal["pass", "blocked", "degraded"]


def aggregate_technical_qc(checks: Sequence[ProviderCheckV1]) -> TechnicalQcSummaryV1:
    """Pure aggregation: counts + verdict (blocked > degraded > pass)."""
    rows = tuple(checks)
    passed = sum(1 for c in rows if c.status == "pass")
    failed = sum(1 for c in rows if c.status == "fail")
    missing = sum(1 for c in rows if c.status == "not_available")
    verdict: Literal["pass", "blocked", "degraded"] = "pass"
    if failed:
        verdict = "blocked"
    elif missing:
        verdict = "degraded"
    return TechnicalQcSummaryV1(
        schema_version="technical-qc-summary-v1",
        checks=rows,
        passed=passed,
        failed=failed,
        not_available=missing,
        verdict=verdict,
    )


def checks_from_run_report(report: object) -> tuple[ProviderCheckV1, ...]:
    """Readback-derived checks whose evidence IS the run report itself.

    Only two checks are derivable without a detector: ``timeline_readback``
    (placements verified against expected record spans) and
    ``conform_source_ranges`` (placement source spans verified). Evidence
    references name the step whose verified readback is the proof
    (``step:<step_id>`` — the ledger rows carry the same ref).
    """
    steps = getattr(report, "steps", ())
    placements = tuple(s for s in steps if getattr(s, "action", "") in _PLACEMENT_ACTIONS)
    if not placements:
        return ()
    all_completed = all(s.status == "completed" for s in placements)
    status: CheckStatus = "pass" if all_completed else "fail"
    first = placements[0].step_id
    detail = (
        f"{len(placements)} placement readbacks verified"
        if all_completed
        else "at least one placement readback failed or was skipped"
    )
    return (
        ProviderCheckV1(
            check="timeline_readback",
            provider="mcp",
            status=status,
            evidence_ref=f"step:{first}",
            detail=detail,
        ),
        ProviderCheckV1(
            check="conform_source_ranges",
            provider="mcp",
            status=status,
            evidence_ref=f"step:{first}",
            detail=detail,
        ),
    )


def to_qc_issue_rows(
    summary: TechnicalQcSummaryV1, *, input_hashes: Sequence[str]
) -> tuple[dict[str, object], ...]:
    """Failed checks as QcIssue-shaped rows referencing provider evidence.

    Rows carry the field names of :class:`services.qc.models.QcIssue`
    (rule_id/severity/detail/evidence/input_hashes) as plain dicts — an
    additive mapping the project QC report embeds; measured values point
    at the provider's evidence reference, never a re-measured number.
    """
    rows: list[dict[str, object]] = []
    for check in summary.checks:
        if check.status != "fail":
            continue
        rule_id = _QC_RULE.get(check.check)
        if rule_id is None:
            continue
        rows.append(
            {
                "rule_id": rule_id,
                "severity": "blocker",
                "detail": (
                    f"{check.provider} provider reported {check.check} failure "
                    f"at {check.evidence_ref}" + (f"; {check.detail}" if check.detail else "")
                ),
                "evidence": {
                    "measured": [
                        {"name": "evidence_ref", "value": check.evidence_ref},
                        {"name": "provider", "value": check.provider},
                    ],
                    "tool_version": f"{check.provider}-server",
                    "threshold_version": "provider-owned",
                },
                "input_hashes": list(input_hashes),
            }
        )
    return tuple(rows)


__all__ = [
    "CheckStatus",
    "ProviderCheckV1",
    "ProviderId",
    "TechnicalCheckId",
    "TechnicalQcSummaryV1",
    "aggregate_technical_qc",
    "checks_from_run_report",
    "to_qc_issue_rows",
]
