"""Event-derived correction classification metrics (Todo 45, Todo 64 family).

Metrics are derived ONLY from the immutable Review Event stream — never from
authored proposal fields or chat history — and are published as typed
artifacts carrying the event lineage they fold. Fixture-derived metrics are
labeled ``fixture_only`` and can NEVER carry a KPI claim: the model has no KPI
field at all, and ``require_no_kpi_claim`` is the guard callers invoke before
any operational use. Technical fixtures prove deterministic mechanics only;
they never measure editorial quality or Active Human Time.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

type ClassificationKey = Literal["clear", "ambiguous", "conflict", "unclassified"]


class ReviewEventView(Protocol):
    """Structural view of one review event (the editorial package must not
    import the review-command machinery; the contract is satisfied
    structurally by ``ReviewEvent0C``)."""

    @property
    def event_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def proposal_json(self) -> str: ...

    @property
    def applied(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...


class CorrectionKindCount(StrictModel):
    kind: str = Field(min_length=1, strict=True)
    count: int = Field(ge=0, strict=True)


class CorrectionMetricsArtifact(StrictModel):
    """Counts folded from one episode's applied/deferred review decisions."""

    schema_version: Literal["correction-metrics-v1"]
    episode_id: Identifier
    fixture_only: bool
    source_event_ids: tuple[Sha256, ...]
    total_applied: int = Field(ge=0, strict=True)
    total_deferred: int = Field(ge=0, strict=True)
    counts_by_classification: dict[str, int]
    structured_ratio_num: int = Field(ge=0, strict=True)
    structured_ratio_den: int = Field(ge=0, strict=True)
    correction_kinds: tuple[CorrectionKindCount, ...]

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)


_CLASSIFICATION_KEYS: tuple[str, ...] = ("clear", "ambiguous", "conflict", "unclassified")


def _kind_of(event_json: str) -> str:
    marker = '"command_kind":"'
    position = event_json.find(marker)
    if position < 0:
        return "unknown"
    start = position + len(marker)
    end = event_json.find('"', start)
    return event_json[start:end] if end > start else "unknown"


def _classification_of(*, reason: str | None) -> ClassificationKey:
    if reason in ("ambiguous", "conflict"):
        return reason
    return "unclassified"


def build_metrics(
    events: tuple[ReviewEventView, ...],
    *,
    episode_id: str,
    fixture_only: bool,
) -> CorrectionMetricsArtifact:
    """Fold one immutable event stream into classification counters."""

    counts = dict.fromkeys(_CLASSIFICATION_KEYS, 0)
    kinds: dict[str, int] = {}
    applied = 0
    deferred = 0
    for event in events:
        if event.kind == "proposal_recorded":
            kind = _kind_of(event.proposal_json)
            kinds[kind] = kinds.get(kind, 0) + 1
            continue
        if event.kind == "decision_applied":
            applied += 1
            counts["clear"] += 1
        else:
            deferred += 1
            counts[_classification_of(reason=event.reason)] += 1
    structured_den = applied + deferred
    return CorrectionMetricsArtifact(
        schema_version="correction-metrics-v1",
        episode_id=episode_id,
        fixture_only=fixture_only,
        source_event_ids=tuple(event.event_id for event in events),
        total_applied=applied,
        total_deferred=deferred,
        counts_by_classification=counts,
        structured_ratio_num=applied,
        structured_ratio_den=structured_den,
        correction_kinds=tuple(
            CorrectionKindCount(kind=kind, count=count)
            for kind, count in sorted(kinds.items())
        ),
    )


def kpi_claim_permitted(metrics: CorrectionMetricsArtifact) -> bool:
    """No KPI claim is derivable from this artifact family yet.

    Fixture-derived metrics are labeled ``fixture_only`` and by design can
    never support a KPI claim; real-episode operational KPI reporting is owned
    by the Todo 64 metrics contract and does not exist yet, so no artifact of
    this version permits one either way.
    """

    del metrics
    return False


__all__ = [
    "CorrectionKindCount",
    "CorrectionMetricsArtifact",
    "build_metrics",
    "kpi_claim_permitted",
]
