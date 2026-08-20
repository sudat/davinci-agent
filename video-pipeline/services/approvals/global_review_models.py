"""Global Review Report v1 models (Todo 65) — separate from F-lane reports.

A Global Review Report is one lane's verdict over one immutable release
candidate: exactly six fixed file names, one canonical schema, bound to one
full Git SHA and one candidate ID. The F-lane ``final-lane-v1`` reports are a
different artifact; nothing here reads or trusts them.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from services.contracts.primitives import Identifier, Sha256, StrictModel

GitFullSha = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", strict=True),
]

ReviewLane = Literal[
    "goal-constraints",
    "code-quality",
    "security",
    "hands-on-qa",
    "context-mining",
    "debugging",
]

REVIEW_LANES: Final[tuple[ReviewLane, ...]] = (
    "goal-constraints",
    "code-quality",
    "security",
    "hands-on-qa",
    "context-mining",
    "debugging",
)

REPORT_FILE_NAMES: Final[tuple[str, ...]] = tuple(
    f"{lane}.json" for lane in REVIEW_LANES
)

GlobalReviewVerdict = Literal["APPROVE", "REJECT"]

GLOBAL_REVIEW_SCHEMA: Final = "global-review-v1"


class GlobalReviewReport(StrictModel):
    """Canonical per-lane report: ``{schema_version, lane, full_sha,
    candidate_id, verdict, raw_response_sha256, artifact_hashes, findings}``."""

    schema_version: Literal["global-review-v1"] = "global-review-v1"
    lane: ReviewLane
    full_sha: GitFullSha
    candidate_id: Identifier
    verdict: GlobalReviewVerdict
    raw_response_sha256: Sha256
    artifact_hashes: dict[str, Sha256] = Field(default_factory=dict)
    findings: tuple[str, ...] = ()


class FinalReviewBinding(StrictModel):
    """FINAL_APPROVED record binding: work, SHA, candidate, report-set hash."""

    work_id: Identifier
    full_sha: GitFullSha
    candidate_id: Identifier
    report_set_sha256: Sha256


__all__ = [
    "GLOBAL_REVIEW_SCHEMA",
    "REPORT_FILE_NAMES",
    "REVIEW_LANES",
    "FinalReviewBinding",
    "GitFullSha",
    "GlobalReviewReport",
    "GlobalReviewVerdict",
    "ReviewLane",
]

