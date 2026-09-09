"""The real-chain run report: models, facts, and the report builder (Todo 46).

The report is the honest machine-readable account of one real-episode run:
which director mode served the selection (deterministic baseline is labeled
as such, live records its served_by), the committed plan hashes, and the
emitted bundle path. Early stops carry ``None`` for the stages not reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from services.contracts.primitives import Sha256, StrictModel

if TYPE_CHECKING:
    from services.cli.real_director import DirectorOutcome
    from services.episode_cockpit.models import EpisodeEditorialGrantV1
    from services.ingest.eligibility import EligibilityResult
    from services.validate.selection_models import SelectionCommitOutcome


class RealChainError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RealChainReport(StrictModel):
    schema_version: Literal["real-chain-report-v1"]
    episode_id: str
    fixture_only: Literal[False]
    stop_stage: str
    eligibility_status: str
    edit_source_world_sha256: Sha256
    director_mode: str | None = None
    director_served_by: str | None = None
    director_transport: str | None = None
    director_request_hash: Sha256 | None = None
    selection_version: int | None = None
    selection_plan_sha256: Sha256 | None = None
    selection_producer: str | None = None
    edit_plan_version: int | None = None
    edit_plan_sha256: Sha256 | None = None
    production_ir_sha256: Sha256 | None = None
    review_plan_sha256: Sha256 | None = None
    review_ir_sha256: Sha256 | None = None
    preview_sha256: Sha256 | None = None
    preview_trace_sha256: Sha256 | None = None
    bundle_path: str | None = None
    editorial_grant_granted: bool | None = None
    editorial_grant_data_class: str | None = None
    editorial_grant_stage: str | None = None
    editorial_grant_granted_at: str | None = None
    editorial_grant_note: str | None = None


@dataclass(frozen=True, slots=True)
class RealRunOutcome:
    report: RealChainReport
    bundle_file: Path | None


@dataclass(frozen=True, slots=True)
class RunFacts:
    episode_id: str
    eligibility: EligibilityResult
    edit_source_world_sha256: str
    director: DirectorOutcome | None = None
    selection_producer: str | None = None
    selection: SelectionCommitOutcome | None = None
    editorial_grant: EpisodeEditorialGrantV1 | None = None


def build_report(facts: RunFacts, stop: str) -> RealChainReport:
    director = facts.director
    selection = facts.selection
    grant = facts.editorial_grant
    return RealChainReport(
        schema_version="real-chain-report-v1",
        episode_id=facts.episode_id,
        fixture_only=False,
        stop_stage=stop,
        eligibility_status=facts.eligibility.status,
        edit_source_world_sha256=facts.edit_source_world_sha256,
        director_mode=director.mode if director is not None else None,
        director_served_by=director.served_by if director is not None else None,
        director_transport=director.transport if director is not None else None,
        director_request_hash=director.request_hash if director is not None else None,
        selection_version=selection.version if selection is not None else None,
        selection_plan_sha256=selection.plan_sha256 if selection is not None else None,
        selection_producer=facts.selection_producer,
        editorial_grant_granted=grant.granted if grant is not None else None,
        editorial_grant_data_class=grant.data_class if grant is not None else None,
        editorial_grant_stage=grant.stage if grant is not None else None,
        editorial_grant_granted_at=grant.granted_at if grant is not None else None,
        editorial_grant_note=grant.note if grant is not None else None,
    )


__all__ = [
    "RealChainError",
    "RealChainReport",
    "RealRunOutcome",
    "RunFacts",
    "build_report",
]
