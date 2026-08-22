"""Additive v4.3 wiring of the seven-domain quality gate into Final Review.

PRD 14.3 / plan task 40: publication is refused while any quality domain
is ``blocked``, and ``manual_fallback_required`` domains must be surfaced
(the Episode Cockpit displays ``surfaced_manual_items``) before final
approval. The gate itself — statuses only, no effect-count quota — lives
in :mod:`services.creative_plan.quality_domains`; this module is the
Final Review binding, following the ``routes.RouteRefusal`` typed
refusal vocabulary.

Additive by design (plan task 40): no existing ``gates/`` or
``final_review/`` module is modified. The gates package has no registry
to join — its ``__init__`` is a static re-export surface and gate
evaluation happens in phase modules — so the registration IS this new
module plus the export surface below.
"""

from __future__ import annotations

from typing import Final

from services.creative_plan.quality_domains import (
    QualityDomainReportV1,
    QualityGateResult,
    evaluate_quality_gate,
)

QUALITY_GATE_ID: Final = "quality-domains-v4.3"


class QualityGateRefusal(Exception):  # noqa: N818 (typed-refusal vocabulary, cf. routes.RouteRefusal)
    """Typed refusal while blocked quality domains remain (PRD 14.3)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def require_quality_gate_pass(report: QualityDomainReportV1) -> QualityGateResult:
    """Final Review precondition on the seven-domain report.

    Raises :class:`QualityGateRefusal` (``quality-domains-blocked``, naming
    every blocked domain) while any domain is blocked; on pass, returns the
    gate result whose ``surfaced_manual_items`` the Cockpit must display
    before final approval.
    """
    result = evaluate_quality_gate(report)
    if result.decision == "reject":
        raise QualityGateRefusal(
            "quality-domains-blocked",
            f"blocked quality domains remain: {', '.join(result.blocked_domains)}",
        )
    return result


__all__ = [
    "QUALITY_GATE_ID",
    "QualityGateRefusal",
    "require_quality_gate_pass",
]
