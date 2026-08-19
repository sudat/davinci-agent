"""Shared issue construction: one factory, one evidence shape per check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from services.qc.models import QcEvidence, QcIssue, QcMeasured, QcRuleId, rule_severity

if TYPE_CHECKING:
    from services.contracts.primitives import RecordFrameSpan


class HasThresholdVersion(Protocol):
    """Read-only structural view; satisfied by any policy-like object."""

    @property
    def threshold_version(self) -> str: ...


@dataclass(frozen=True, slots=True)
class IssueFactory:
    """Closes over the policy threshold version and input hashes for one run."""

    threshold_version: str
    inputs: tuple[str, ...]

    @classmethod
    def for_policy(cls, policy: HasThresholdVersion, inputs: tuple[str, ...]) -> IssueFactory:
        return cls(threshold_version=policy.threshold_version, inputs=inputs)

    def build(  # noqa: PLR0913 (one evidence-carrying issue constructor)
        self,
        rule_id: QcRuleId,
        detail: str,
        tool_version: str,
        measured: tuple[QcMeasured, ...],
        *,
        item_id: str | None = None,
        decision_id: str | None = None,
        record_span: RecordFrameSpan | None = None,
    ) -> QcIssue:
        return QcIssue(
            rule_id=rule_id,
            severity=rule_severity(rule_id),
            detail=detail,
            item_id=item_id,
            decision_id=decision_id,
            record_span=record_span,
            evidence=QcEvidence(
                measured=measured,
                tool_version=tool_version,
                threshold_version=self.threshold_version,
            ),
            input_hashes=self.inputs,
        )


__all__ = ["IssueFactory"]
