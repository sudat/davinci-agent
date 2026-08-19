"""Todo-53 final-review rig: deterministic bundles, records, and ledgers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.approvals.ingress import record_fixture_operation
from services.approvals.models import OperationDraft
from services.approvals.store import OperationRecordStore
from services.qc.models import (
    QcEvidence,
    QcInputBinding,
    QcIssue,
    QcMeasured,
    QcReport,
    QcToolVersions,
    UnresolvedHumanGate,
    rule_severity,
)
from services.qc.privacy_gate import (
    DeclaredPrivacyIssue,
    PrivacyDeclarations,
    PrivacyResolution,
)

if TYPE_CHECKING:
    from services.approvals.models import ChainedOperationRecord

EPISODE = "ep-final-review"
ACTOR = "test-operator"
THRESHOLD = "qc-thresholds-test-v1"
_PURPOSE_TARGET_TYPES = {
    "editorial": "edit-plan",
    "final": "final-render",
    "manual_freeze": "frozen-timeline",
}


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def versions() -> QcToolVersions:
    return QcToolVersions(
        qc_engine="todo52-v1", ffmpeg_sha256=sha("ffmpeg"), ffprobe_sha256=sha("ffprobe")
    )


def blocker_issue() -> QcIssue:
    return QcIssue(
        rule_id="video_black_span",
        severity="blocker",
        detail="critical finding",
        evidence=QcEvidence(
            measured=(QcMeasured(name="span_ms", value="1200"),),
            tool_version="t",
            threshold_version=THRESHOLD,
        ),
        input_hashes=(sha("render"),),
    )


def privacy_gate(issue_id: str) -> UnresolvedHumanGate:
    return UnresolvedHumanGate(
        gate_id=f"privacy-rights-{issue_id}",
        rule_id="privacy_rights_unresolved",
        source="privacy_declaration",
        detail="awaiting operator",
    )


def passed_qc_report() -> QcReport:
    return QcReport(
        schema_version="qc-report-v1",
        verdict="passed",
        issues=(),
        unresolved_human_gates=(),
        tool_versions=versions(),
        threshold_version=THRESHOLD,
        inputs=(QcInputBinding(kind="render", sha256=sha("render")),),
    )


def critical_blocked_qc_report() -> QcReport:
    return QcReport(
        schema_version="qc-report-v1",
        verdict="blocked",
        issues=(blocker_issue(),),
        unresolved_human_gates=(),
        tool_versions=versions(),
        threshold_version=THRESHOLD,
        inputs=(QcInputBinding(kind="render", sha256=sha("render")),),
    )


def privacy_blocked_qc_report() -> QcReport:
    return QcReport(
        schema_version="qc-report-v1",
        verdict="blocked",
        issues=(
            QcIssue(
                rule_id="privacy_rights_unresolved",
                severity=rule_severity("privacy_rights_unresolved"),
                detail="declared privacy issue issue-1 awaits an operator resolution",
                evidence=QcEvidence(
                    measured=(QcMeasured(name="category", value="privacy"),),
                    tool_version="privacy-gate-v1",
                    threshold_version=THRESHOLD,
                ),
                input_hashes=(sha("render"),),
            ),
        ),
        unresolved_human_gates=(privacy_gate("issue-1"),),
        tool_versions=versions(),
        threshold_version=THRESHOLD,
        inputs=(QcInputBinding(kind="render", sha256=sha("render")),),
    )


def empty_declarations() -> PrivacyDeclarations:
    return PrivacyDeclarations.empty()


def unresolved_declarations() -> PrivacyDeclarations:
    return PrivacyDeclarations(
        schema_version="privacy-declarations-v1",
        declared_issues=(
            DeclaredPrivacyIssue(
                issue_id="issue-1",
                category="privacy",
                declared_by="local-operator",
                detail="passer-by in frame 120",
                fixture_only=True,
            ),
        ),
    )


def resolved_declarations() -> PrivacyDeclarations:
    return PrivacyDeclarations(
        schema_version="privacy-declarations-v1",
        declared_issues=(
            DeclaredPrivacyIssue(
                issue_id="issue-1",
                category="privacy",
                declared_by="local-operator",
                detail="passer-by in frame 120",
                fixture_only=True,
                resolution=PrivacyResolution(
                    resolved_by="local-operator",
                    decision="masked_in_output",
                    record_sha256=sha("privacy-resolution-record"),
                    fixture_only=True,
                ),
            ),
        ),
    )


def make_records_store(tmp_path: Path) -> OperationRecordStore:
    return OperationRecordStore(tmp_path / "operation-records.jsonl")


def _draft(
    *,
    purpose: str,
    target_hash: str,
    decision: str,
    runner_class: str,
    fixture_only: bool,
) -> OperationDraft:
    return OperationDraft.model_validate(
        {
            "purpose": purpose,
            "target_type": _PURPOSE_TARGET_TYPES[purpose],
            "target_hash": target_hash,
            "decision": decision,
            "actor_id": ACTOR,
            "uid": None if fixture_only else 1000,
            "tty": None if fixture_only else "/dev/pts/0",
            "wall_time_unix": None,
            "fixture_only": fixture_only,
            "runner_class": runner_class,
        }
    )


def fixture_record(
    store: OperationRecordStore,
    *,
    purpose: str,
    target_hash: str,
    decision: str = "approve",
    runner_class: str = "automation",
) -> ChainedOperationRecord:
    draft = record_fixture_operation(
        purpose=purpose,  # pyright: ignore[reportArgumentType]
        target_bundle_hash=target_hash,
        decision=decision,  # pyright: ignore[reportArgumentType]
        actor_id=ACTOR,
        runner_class=runner_class,  # pyright: ignore[reportArgumentType]
    )
    return store.append(draft)


def operator_record(
    store: OperationRecordStore,
    *,
    purpose: str,
    target_hash: str,
    decision: str = "approve",
) -> ChainedOperationRecord:
    draft = _draft(
        purpose=purpose,
        target_hash=target_hash,
        decision=decision,
        runner_class="operator",
        fixture_only=False,
    )
    return store.append(draft)
