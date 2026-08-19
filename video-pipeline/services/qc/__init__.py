"""Deterministic QC engine (Todo 52): IR/Preview/video/audio/subtitle gates.

Privacy/Rights handling is declaration-in / human-gate-out only — this package
never detects or ranks privacy candidates (Phase 4 scope guard).
"""

from __future__ import annotations

from services.qc.models import (
    QC_ENGINE_VERSION,
    QcEvidence,
    QcInputBinding,
    QcIssue,
    QcMeasured,
    QcPolicy,
    QcReport,
    QcRuleId,
    QcSeverity,
    QcToolVersions,
    QcVerdict,
    UnresolvedHumanGate,
    compute_verdict,
    rule_severity,
)

__all__ = [
    "QC_ENGINE_VERSION",
    "QcEvidence",
    "QcInputBinding",
    "QcIssue",
    "QcMeasured",
    "QcPolicy",
    "QcReport",
    "QcRuleId",
    "QcSeverity",
    "QcToolVersions",
    "QcVerdict",
    "UnresolvedHumanGate",
    "compute_verdict",
    "rule_severity",
]
