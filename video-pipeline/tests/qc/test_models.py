"""QcReport/QcPolicy contracts: verdict guard, canonical hashes, severities."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.qc.models import (
    QC_ENGINE_VERSION,
    QcEvidence,
    QcInputBinding,
    QcIssue,
    QcMeasured,
    QcReport,
    QcToolVersions,
    UnresolvedHumanGate,
    compute_verdict,
    rule_severity,
)
from tests.qc.support import clean_policy, preset


def _versions() -> QcToolVersions:
    return QcToolVersions(
        qc_engine=QC_ENGINE_VERSION,
        ffmpeg_sha256="a" * 64,
        ffprobe_sha256="b" * 64,
    )


def _blocker() -> QcIssue:
    return QcIssue(
        rule_id="video_black_span",
        severity="blocker",
        detail="black span",
        evidence=QcEvidence(
            measured=(QcMeasured(name="black_start_ms", value="0"),),
            tool_version="t",
            threshold_version="qc-thresholds-test-v1",
        ),
        input_hashes=("c" * 64,),
    )


def _gate() -> UnresolvedHumanGate:
    return UnresolvedHumanGate(
        gate_id="privacy-rights-issue-1",
        rule_id="privacy_rights_unresolved",
        source="privacy_declaration",
        detail="awaiting operator",
    )


def test_verdict_passed_with_blocker_is_invalid() -> None:
    with pytest.raises(ValidationError, match="verdict"):
        QcReport(
            schema_version="qc-report-v1",
            verdict="passed",
            issues=(_blocker(),),
            unresolved_human_gates=(),
            tool_versions=_versions(),
            threshold_version="qc-thresholds-test-v1",
            inputs=(QcInputBinding(kind="render", sha256="c" * 64),),
        )


def test_verdict_passed_with_unresolved_gate_is_invalid() -> None:
    with pytest.raises(ValidationError, match="verdict"):
        QcReport(
            schema_version="qc-report-v1",
            verdict="passed",
            issues=(),
            unresolved_human_gates=(_gate(),),
            tool_versions=_versions(),
            threshold_version="qc-thresholds-test-v1",
            inputs=(QcInputBinding(kind="render", sha256="c" * 64),),
        )


def test_blocked_report_represented_and_computed() -> None:
    report = QcReport(
        schema_version="qc-report-v1",
        verdict="blocked",
        issues=(_blocker(),),
        unresolved_human_gates=(_gate(),),
        tool_versions=_versions(),
        threshold_version="qc-thresholds-test-v1",
        inputs=(QcInputBinding(kind="render", sha256="c" * 64),),
    )
    assert report.verdict == "blocked"
    assert compute_verdict((_blocker(),), ()) == "blocked"
    assert compute_verdict((), ()) == "passed"


def test_minor_resolved_issue_does_not_block_but_is_recorded() -> None:
    resolved = QcIssue(
        rule_id="privacy_rights_resolved",
        severity=rule_severity("privacy_rights_resolved"),
        detail="resolved",
        evidence=QcEvidence(
            measured=(QcMeasured(name="decision", value="removed_from_output"),),
            tool_version="privacy-gate-v1",
            threshold_version="qc-thresholds-test-v1",
        ),
        input_hashes=("c" * 64,),
    )
    assert resolved.severity == "minor"
    assert compute_verdict((resolved,), ()) == "passed"


def test_policy_hash_binds_canonical_bytes() -> None:
    policy = clean_policy(preset())
    assert policy.verify_hash()
    tampered = policy.model_copy(update={"threshold_version": "other-v1"})
    assert not tampered.verify_hash()
    payload = json.loads(canonical_model_bytes(policy))
    assert payload["schema_version"] == "resolved-qc-policy-v1"
    assert payload["policy_sha256"] == policy.policy_sha256
