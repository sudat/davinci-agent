"""Assembly and privacy alignment for the sealed Final Review bundle.

A bundle freezes everything the operator approves at FINAL_APPROVED: the
render hash, build-output hash, conformance fingerprint, QC report hash,
the manually-declared Privacy/Rights entries with their resolution state,
and the diff hashes against the EDITORIAL_APPROVED checkpoint bundle. The
``target_set_hash`` is the displayed target set: any change to the bundle
changes it, so dependent FINAL records cannot silently survive a rebuild.
Privacy entries derive ONLY from the Todo-52 operator declarations —
assembly never detects, ranks, or dismisses anything, and a QC "passed"
label over unresolved declarations is refused as a disguised dismissal.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from services.contracts.serialization import GENESIS_SHA256
from services.final_review.bundle_models import (
    FINAL_REVIEW_BUNDLE_SCHEMA,
    BundlePrivacyEntry,
    DiffComponent,
    EditorialDiff,
    FinalReviewBundle,
    ResolutionState,
)
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.qc.models import QcReport
    from services.qc.privacy_gate import PrivacyDeclarations

__all__ = [
    "FINAL_REVIEW_BUNDLE_SCHEMA",
    "BundlePrivacyEntry",
    "DiffComponent",
    "EditorialDiff",
    "FinalReviewBundle",
    "PrivacyAlignmentError",
    "ResolutionState",
    "assemble_final_review_bundle",
    "verify_privacy_alignment",
]


class PrivacyAlignmentError(Exception):
    """A bundle misrepresents the operator privacy declarations."""


def _entries_from_declarations(
    declarations: PrivacyDeclarations,
) -> tuple[BundlePrivacyEntry, ...]:
    entries: list[BundlePrivacyEntry] = []
    for declared in declarations.declared_issues:
        if declared.resolution is None:
            entries.append(
                BundlePrivacyEntry(
                    issue_id=declared.issue_id,
                    category=declared.category,
                    declared_by=declared.declared_by,
                    detail=declared.detail,
                    resolution_state="unresolved",
                )
            )
        else:
            entries.append(
                BundlePrivacyEntry(
                    issue_id=declared.issue_id,
                    category=declared.category,
                    declared_by=declared.declared_by,
                    detail=declared.detail,
                    resolution_state=declared.resolution.decision,
                    resolution_record_sha256=declared.resolution.record_sha256,
                )
            )
    return tuple(entries)


def assemble_final_review_bundle(  # noqa: PLR0913 (one slot per sealed input authority)
    *,
    episode_id: str,
    fixture_only: bool,
    render_sha256: str,
    build_output_sha256: str,
    conformance_fingerprint: str,
    qc_report: QcReport,
    privacy_declarations: PrivacyDeclarations,
    editorial_diff: EditorialDiff | dict[str, object],
) -> FinalReviewBundle:
    entries = _entries_from_declarations(privacy_declarations)
    unresolved = any(entry.resolution_state == "unresolved" for entry in entries)
    if unresolved and qc_report.verdict == "passed":
        raise PrivacyAlignmentError(
            "privacy-dismissed-by-qc-label: the declared privacy entries are "
            "unresolved, so a QC verdict of passed is a disguised dismissal; "
            "route the declarations through the Todo-52 privacy gate first"
        )
    diff = (
        editorial_diff
        if isinstance(editorial_diff, EditorialDiff)
        else EditorialDiff.model_validate(editorial_diff)
    )
    unsealed = FinalReviewBundle.model_construct(
        schema_version=FINAL_REVIEW_BUNDLE_SCHEMA,
        episode_id=episode_id,
        fixture_only=fixture_only,
        render_sha256=render_sha256,
        build_output_sha256=build_output_sha256,
        conformance_fingerprint=conformance_fingerprint,
        qc_report_sha256=hashlib.sha256(canonical_model_bytes(qc_report)).hexdigest(),
        qc_verdict=qc_report.verdict,
        privacy_rights=entries,
        editorial_diff=diff,
        target_set_hash=GENESIS_SHA256,
        bundle_sha256=GENESIS_SHA256,
    )
    retargeted = unsealed.model_copy(
        update={"target_set_hash": unsealed.computed_target_set_hash()}
    )
    sealed = retargeted.model_copy(
        update={"bundle_sha256": retargeted.computed_bundle_hash()}
    )
    return FinalReviewBundle.model_validate_json(sealed.model_dump_json())


def verify_privacy_alignment(
    bundle: FinalReviewBundle, declarations: PrivacyDeclarations
) -> None:
    """Refuse dismissed or flipped declarations vs the sealed bundle."""

    by_id = {entry.issue_id: entry for entry in bundle.privacy_rights}
    declared_ids = {declared.issue_id for declared in declarations.declared_issues}
    for entry in bundle.privacy_rights:
        if entry.issue_id not in declared_ids:
            raise PrivacyAlignmentError(
                f"privacy-dismissed: bundle entry {entry.issue_id} has no "
                "matching operator declaration; dropping declarations after "
                "the seal is a dismissal"
            )
    for declared in declarations.declared_issues:
        entry = by_id.get(declared.issue_id)
        if entry is None:
            raise PrivacyAlignmentError(
                f"privacy-dismissed: declared issue {declared.issue_id} is absent "
                "from the bundle; automation may never dismiss a declaration"
            )
        expected: ResolutionState = (
            "unresolved"
            if declared.resolution is None
            else declared.resolution.decision
        )
        if entry.resolution_state != expected:
            raise PrivacyAlignmentError(
                f"resolution-flipped: issue {declared.issue_id} is "
                f"{entry.resolution_state} in the bundle but {expected} in the "
                "declarations"
            )
        if declared.resolution is not None and (
            entry.resolution_record_sha256 != declared.resolution.record_sha256
        ):
            raise PrivacyAlignmentError(
                f"resolution-record-mismatch: issue {declared.issue_id} cites a "
                "different resolution record than the declarations"
            )
