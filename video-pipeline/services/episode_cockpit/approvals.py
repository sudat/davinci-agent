"""Approval bundling into at most two normal blocking sessions (PRD 13.4).

Compatible internal approvals group into AT MOST two normal blocking
review sessions: ``editorial-presentation`` (editorial meaning and its
presentation surface) and ``final-publication`` (final render and
publication authority). Privacy, rights, and manual-freeze approvals can
never merge into those sessions — each exception class becomes its own
extra bundle carrying a grouped explanation, so it surfaces as an
explained additional stop rather than a silent fold into a normal
session.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError

# Runtime import (NOT TYPE_CHECKING): pydantic resolves ApprovalPurpose
# while building the StrictModel fields below, at class-definition time.
from services.approvals.models import ApprovalPurpose  # noqa: TC001
from services.contracts.primitives import Identifier, Sha256, StrictModel

SessionKey = Literal[
    "editorial-presentation",
    "final-publication",
    "privacy",
    "rights",
    "manual_freeze",
]

BundleKind = Literal["normal", "exception"]


class PendingApproval(StrictModel):
    """One approval awaiting an operator decision (a store record id)."""

    record_id: Identifier
    purpose: ApprovalPurpose
    target_hash: Sha256


class ApprovalBundle(StrictModel):
    """One blocking review session over one or more compatible approvals."""

    session_key: SessionKey
    kind: BundleKind
    purposes: tuple[ApprovalPurpose, ...]
    items: tuple[PendingApproval, ...]
    explanation: str | None = None

    @model_validator(mode="after")
    def enforce_explanation_rules(self) -> ApprovalBundle:
        if not self.items:
            raise PydanticCustomError("bundle_empty", "a bundle holds at least one approval")
        if self.kind == "exception" and not self.explanation:
            raise PydanticCustomError(
                "explanation_missing", "exception bundles carry a grouped explanation"
            )
        if self.kind == "normal" and self.explanation is not None:
            raise PydanticCustomError(
                "explanation_forbidden", "normal bundles never carry an exception explanation"
            )
        return self


class ApprovalBundles(StrictModel):
    """The full bundling outcome for one episode's pending approvals."""

    schema_version: Literal["cockpit-approval-bundles-v1"] = "cockpit-approval-bundles-v1"
    bundles: tuple[ApprovalBundle, ...]

    @model_validator(mode="after")
    def enforce_one_bundle_per_session(self) -> ApprovalBundles:
        keys = [bundle.session_key for bundle in self.bundles]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError(
                "duplicate_session", "at most one bundle per session key"
            )
        return self


_NORMAL_SESSIONS: tuple[tuple[SessionKey, frozenset[str]], ...] = (
    ("editorial-presentation", frozenset({"editorial", "presentation"})),
    ("final-publication", frozenset({"final", "publication"})),
)

ExceptionPurpose = Literal["privacy", "rights", "manual_freeze"]

_EXCEPTION_SESSIONS: tuple[ExceptionPurpose, ...] = (
    "privacy",
    "rights",
    "manual_freeze",
)

_EXCEPTION_EXPLANATIONS: dict[ExceptionPurpose, str] = {
    "privacy": (
        "プライバシー確認は他の承認と併合できません。"
        "対象範囲を確認して、この停止で個別に判断してください。"
    ),
    "rights": (
        "権利確認は他の承認と併合できません。"
        "権利対象と利用条件を確認して、この停止で個別に判断してください。"
    ),
    "manual_freeze": (
        "手動確定(Manual Finalization)の確認は再現性に関わるため、"
        "他の承認と併合できません。"
    ),
}


def bundle_approvals(
    pending: Sequence[PendingApproval | dict[str, object]],
) -> ApprovalBundles:
    """Group pending approvals into the two normal sessions plus exceptions.

    Normal sessions are emitted in pipeline order and only when non-empty;
    each exception class present becomes one extra bundle whose grouped
    explanation states why it cannot merge. Input order is preserved
    within a bundle, and pending entries supplied as raw dicts are parsed
    exactly once here (boundary discipline).
    """

    parsed: list[PendingApproval] = [
        item if isinstance(item, PendingApproval) else PendingApproval.model_validate(item)
        for item in pending
    ]
    bundles: list[ApprovalBundle] = []
    for session_key, purposes in _NORMAL_SESSIONS:
        members = [item for item in parsed if item.purpose in purposes]
        if members:
            bundles.append(
                ApprovalBundle(
                    session_key=session_key,
                    kind="normal",
                    purposes=tuple(item.purpose for item in members),
                    items=tuple(members),
                )
            )
    for purpose in _EXCEPTION_SESSIONS:
        members = [item for item in parsed if item.purpose == purpose]
        if members:
            bundles.append(
                ApprovalBundle(
                    session_key=purpose,
                    kind="exception",
                    purposes=(purpose,),
                    items=tuple(members),
                    explanation=_EXCEPTION_EXPLANATIONS[purpose],
                )
            )
    return ApprovalBundles(bundles=tuple(bundles))


def count_blocking_sessions(bundles: ApprovalBundles) -> int:
    """The blocking-session count behind the PRD 13.4 'at most two' metric."""

    return len(bundles.bundles)


__all__ = [
    "ApprovalBundle",
    "ApprovalBundles",
    "BundleKind",
    "PendingApproval",
    "SessionKey",
    "bundle_approvals",
    "count_blocking_sessions",
]
