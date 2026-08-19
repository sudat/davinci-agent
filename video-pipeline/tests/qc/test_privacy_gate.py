"""Privacy gate: declaration in / human gate out; automated detection rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

import services.qc.privacy_gate as gate_module
from services.qc.privacy_gate import (
    DeclaredPrivacyIssue,
    PrivacyDeclarations,
    PrivacyResolution,
    PrivacyScopeError,
    evaluate_privacy_gate,
    guard_no_automated_privacy_detectors,
)

INPUTS = ("5" * 64,)
THRESHOLD = "qc-thresholds-test-v1"


def test_unresolved_declaration_blocks_behind_human_gate() -> None:
    declarations = PrivacyDeclarations(
        schema_version="privacy-declarations-v1",
        declared_issues=(
            DeclaredPrivacyIssue(
                issue_id="face-in-b-roll",
                category="privacy",
                declared_by="operator",
                detail="a bystander face is visible in the b-roll",
                fixture_only=True,
                resolution=None,
            ),
        ),
    )
    issues, gates = evaluate_privacy_gate(declarations, THRESHOLD, INPUTS)
    assert {issue.rule_id for issue in issues} == {"privacy_rights_unresolved"}
    assert issues[0].severity == "blocker"
    assert len(gates) == 1
    assert gates[0].gate_id == "privacy-rights-face-in-b-roll"
    assert gates[0].rule_id == "privacy_rights_unresolved"


def test_resolved_declaration_records_resolved_and_does_not_gate() -> None:
    declarations = PrivacyDeclarations(
        schema_version="privacy-declarations-v1",
        declared_issues=(
            DeclaredPrivacyIssue(
                issue_id="bgm-license",
                category="rights",
                declared_by="operator",
                detail="background music license covers this channel",
                fixture_only=True,
                resolution=PrivacyResolution(
                    resolved_by="operator",
                    decision="cleared_for_publication",
                    record_sha256="a" * 64,
                    fixture_only=True,
                ),
            ),
        ),
    )
    issues, gates = evaluate_privacy_gate(declarations, THRESHOLD, INPUTS)
    assert gates == ()
    assert {issue.rule_id for issue in issues} == {"privacy_rights_resolved"}
    assert issues[0].severity == "minor"


def test_empty_declarations_produce_nothing() -> None:
    issues, gates = evaluate_privacy_gate(PrivacyDeclarations.empty(), THRESHOLD, INPUTS)
    assert issues == ()
    assert gates == ()


def test_scope_guard_rejects_detector_imports_in_probe_modules() -> None:
    with pytest.raises(PrivacyScopeError, match="cv2"):
        guard_no_automated_privacy_detectors("import cv2\n", "probe-a")
    with pytest.raises(PrivacyScopeError, match="easyocr"):
        guard_no_automated_privacy_detectors("import easyocr\n", "probe-b")
    with pytest.raises(PrivacyScopeError, match="plate"):
        guard_no_automated_privacy_detectors(
            "from licence_plate_detector import scan\n", "probe-c"
        )


def test_scope_guard_rejects_detector_definitions_in_probe_modules() -> None:
    with pytest.raises(PrivacyScopeError, match="detect_"):
        guard_no_automated_privacy_detectors(
            "def detect_faces(frame):\n    return []\n", "probe-d"
        )
    with pytest.raises(PrivacyScopeError, match="scan_privacy"):
        guard_no_automated_privacy_detectors(
            "def scan_privacy(media):\n    return []\n", "probe-e"
        )


def test_scope_guard_accepts_the_qc_package_itself() -> None:
    for source in sorted((Path(__file__).parents[2] / "services" / "qc").rglob("*.py")):
        guard_no_automated_privacy_detectors(
            source.read_text(encoding="utf-8"), f"services/qc/{source.name}"
        )


def test_gate_module_defines_no_detection_surface() -> None:
    public = [name for name in dir(gate_module) if not name.startswith("_")]
    assert not [name for name in public if name.startswith("detect_")]
    assert "scan_privacy" not in public
