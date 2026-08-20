"""Six-file Global Review Report v1 set loading and typed validation.

``load_report_set`` accepts a report directory only when it holds exactly
the six fixed-name reports, every verdict is APPROVE, one identical full SHA
(the declared work state), and one identical candidate ID. It returns the
canonical report-set hash that the FINAL_APPROVED operation record binds.
``load_revocations`` reads the local candidate revocation list. Every
failure is a typed ``GlobalReviewError``; nothing here writes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import Field, ValidationError

from services.approvals.global_review_models import (
    REPORT_FILE_NAMES,
    REVIEW_LANES,
    FinalReviewBinding,
    GlobalReviewReport,
)
from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import canonical_model_bytes

DEFAULT_REVOCATIONS_PATH: Final = Path("config/approvals/candidate-revocations.json")


class GlobalReviewError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class LoadedReportSet:
    reports: tuple[GlobalReviewReport, ...]
    report_set_sha256: str

    def binding(self, work_id: str) -> FinalReviewBinding:
        return FinalReviewBinding(
            work_id=work_id,
            full_sha=self.reports[0].full_sha,
            candidate_id=self.reports[0].candidate_id,
            report_set_sha256=self.report_set_sha256,
        )


class RevokedCandidate(StrictModel):
    candidate_id: Identifier
    reason: str = ""


class RevocationList(StrictModel):
    schema_version: str = "candidate-revocations-v1"
    revoked: tuple[RevokedCandidate, ...] = Field(default_factory=tuple)

    def ids(self) -> frozenset[str]:
        return frozenset(entry.candidate_id for entry in self.revoked)


def _read_reports(report_dir: Path) -> list[GlobalReviewReport]:
    present = sorted(entry.name for entry in report_dir.iterdir())
    unexpected = [name for name in present if name not in REPORT_FILE_NAMES]
    if unexpected:
        raise GlobalReviewError(
            "extra-report",
            f"only the six fixed report names are allowed; found {unexpected}",
        )
    missing = [name for name in REPORT_FILE_NAMES if name not in present]
    if missing:
        raise GlobalReviewError(
            "missing-report", f"the report set is incomplete; missing {missing}"
        )
    reports: list[GlobalReviewReport] = []
    for name in REPORT_FILE_NAMES:
        try:
            reports.append(
                GlobalReviewReport.model_validate_json((report_dir / name).read_bytes())
            )
        except (OSError, ValidationError) as error:
            raise GlobalReviewError(
                "malformed-report",
                f"{name} is not a canonical global-review-v1 file: {error}",
            ) from error
        if reports[-1].lane + ".json" != name:
            raise GlobalReviewError(
                "lane-name-mismatch",
                f"{name} declares lane {reports[-1].lane}; each lane must map to its own file",
            )
    return reports


def _validate_bindings(
    reports: list[GlobalReviewReport], *, full_sha: str, candidate_id: str
) -> None:
    lanes = [report.lane for report in reports]
    if sorted(lanes) != sorted(REVIEW_LANES):
        raise GlobalReviewError(
            "duplicate-lane", f"lanes must be the six distinct lanes once each: {lanes}"
        )
    shas = {report.full_sha for report in reports}
    if len(shas) != 1:
        raise GlobalReviewError(
            "mixed-sha", f"reports bind different full SHAs: {sorted(shas)}"
        )
    if reports[0].full_sha != full_sha:
        raise GlobalReviewError(
            "stale-report",
            f"reports bind {reports[0].full_sha}, not the declared work state {full_sha}",
        )
    candidates = {report.candidate_id for report in reports}
    if len(candidates) != 1:
        raise GlobalReviewError(
            "mixed-candidate",
            f"reports bind different candidate IDs: {sorted(candidates)}",
        )
    if reports[0].candidate_id != candidate_id:
        raise GlobalReviewError(
            "candidate-mismatch",
            f"reports bind {reports[0].candidate_id}, not the declared {candidate_id}",
        )
    rejected = [report.lane for report in reports if report.verdict != "APPROVE"]
    if rejected:
        raise GlobalReviewError(
            "verdict-not-approved",
            f"every report must be APPROVE; rejected by {rejected}",
        )


def load_report_set(report_dir: Path, *, full_sha: str, candidate_id: str) -> LoadedReportSet:
    if not report_dir.is_dir():
        raise GlobalReviewError(
            "report-dir-unreadable", f"report directory not found: {report_dir}"
        )
    reports = _read_reports(report_dir)
    _validate_bindings(reports, full_sha=full_sha, candidate_id=candidate_id)
    return LoadedReportSet(reports=tuple(reports), report_set_sha256=_set_hash(reports))


def _set_hash(reports: list[GlobalReviewReport]) -> str:
    digest = hashlib.sha256()
    for report in sorted(reports, key=lambda item: item.lane):
        digest.update(report.lane.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(canonical_model_bytes(report)).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def load_revocations(path: Path) -> frozenset[str]:
    try:
        payload = RevocationList.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise GlobalReviewError(
            "revocations-invalid",
            f"revocation list {path} is unreadable/malformed: {error}",
        ) from error
    return payload.ids()


__all__ = [
    "DEFAULT_REVOCATIONS_PATH",
    "GlobalReviewError",
    "LoadedReportSet",
    "RevocationList",
    "load_report_set",
    "load_revocations",
]
