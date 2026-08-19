"""FinalReviewBundle: hash-sealed, target-set-bound, privacy-aligned."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.final_review.bundle import (
    BundlePrivacyEntry,
    FinalReviewBundle,
    PrivacyAlignmentError,
    assemble_final_review_bundle,
    verify_privacy_alignment,
)

if TYPE_CHECKING:
    from services.qc.models import QcReport
    from services.qc.privacy_gate import PrivacyDeclarations

from tests.final_review.support import (
    EPISODE,
    critical_blocked_qc_report,
    empty_declarations,
    passed_qc_report,
    privacy_blocked_qc_report,
    resolved_declarations,
    sha,
    unresolved_declarations,
)


def editorial_diff() -> dict[str, object]:
    return {
        "checkpoint_target_set_hash": sha("checkpoint"),
        "components": (
            {
                "component": "render",
                "checkpoint_sha256": sha("checkpoint-preview"),
                "final_sha256": sha("final-render"),
            },
        ),
    }


def make_bundle(
    *,
    render_sha256: str = sha("final-render"),
    qc_report: QcReport | None = None,
    privacy_declarations: PrivacyDeclarations | None = None,
) -> FinalReviewBundle:
    return assemble_final_review_bundle(
        episode_id=EPISODE,
        fixture_only=True,
        render_sha256=render_sha256,
        build_output_sha256=sha("build-output"),
        conformance_fingerprint=sha("conformance"),
        qc_report=passed_qc_report() if qc_report is None else qc_report,
        privacy_declarations=(
            empty_declarations() if privacy_declarations is None else privacy_declarations
        ),
        editorial_diff=editorial_diff(),
    )


def test_bundle_is_sealed_and_target_set_deterministic() -> None:
    bundle = make_bundle()
    assert bundle.bundle_sha256 == bundle.computed_bundle_hash()
    assert bundle.target_set_hash == bundle.computed_target_set_hash()
    again = make_bundle()
    assert again == bundle


def test_bundle_change_changes_displayed_target_set() -> None:
    base = make_bundle()
    changed = make_bundle(render_sha256=sha("final-render-2"))
    assert changed.target_set_hash != base.target_set_hash
    assert changed.bundle_sha256 != base.bundle_sha256


def test_bundle_tamper_is_refused_at_validation() -> None:
    bundle = make_bundle()
    payload = bundle.model_dump()
    payload["render_sha256"] = sha("evil")
    with pytest.raises(ValidationError, match="seal"):
        FinalReviewBundle.model_validate(payload)


def test_resolved_privacy_entry_requires_resolution_record() -> None:
    with pytest.raises(ValidationError, match="resolution_record"):
        BundlePrivacyEntry(
            issue_id="issue-1",
            category="privacy",
            declared_by="local-operator",
            detail="d",
            resolution_state="masked_in_output",
            resolution_record_sha256=None,
        )


def test_assemble_carries_resolved_and_unresolved_privacy_state() -> None:
    unresolved = make_bundle(
        qc_report=privacy_blocked_qc_report(),
        privacy_declarations=unresolved_declarations(),
    )
    assert unresolved.qc_verdict == "blocked"
    assert unresolved.privacy_rights[0].resolution_state == "unresolved"
    resolved = make_bundle(privacy_declarations=resolved_declarations())
    entry = resolved.privacy_rights[0]
    assert entry.resolution_state == "masked_in_output"
    assert entry.resolution_record_sha256 == sha("privacy-resolution-record")


def test_assemble_carries_blocked_verdict_for_critical_findings() -> None:
    bundle = make_bundle(qc_report=critical_blocked_qc_report())
    assert bundle.qc_verdict == "blocked"


def test_assemble_refuses_passed_qc_label_over_unresolved_privacy() -> None:
    with pytest.raises(PrivacyAlignmentError, match="privacy"):
        make_bundle(privacy_declarations=unresolved_declarations())


def test_alignment_refuses_dismissed_privacy_entry() -> None:
    empty_bundle = make_bundle()
    with pytest.raises(PrivacyAlignmentError, match="dismiss"):
        verify_privacy_alignment(empty_bundle, unresolved_declarations())


def test_alignment_refuses_resolution_flip_without_record() -> None:
    declared_unresolved = make_bundle(
        qc_report=privacy_blocked_qc_report(),
        privacy_declarations=unresolved_declarations(),
    )
    resolved_bundle = make_bundle(privacy_declarations=resolved_declarations())
    with pytest.raises(PrivacyAlignmentError, match="resolution"):
        verify_privacy_alignment(resolved_bundle, unresolved_declarations())
    verify_privacy_alignment(declared_unresolved, unresolved_declarations())
