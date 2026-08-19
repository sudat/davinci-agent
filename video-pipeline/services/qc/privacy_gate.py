"""Manually declared Privacy/Rights issues: declaration in, human gate out.

This module NEVER detects, scans, ranks, or classifies privacy candidates —
that is Phase-4 scope and is rejected here. It only consumes operator
declarations: a declared issue without a resolution record blocks QC behind
an unresolved human gate; a structurally valid resolution artifact records
the issue resolved (minor, non-blocking evidence).

``guard_no_automated_privacy_detectors`` enforces the scope boundary on
source text: importing detection roots (cv2 / face / plate / OCR engines) or
defining ``detect_*`` / ``scan_privacy`` callables anywhere under
``services/qc`` is a hard error. The guard runs as part of every QC run and
is exercised by the Todo-52 scope-guard tests.
"""

from __future__ import annotations

import ast
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Identifier, RecordFrameSpan, Sha256, StrictModel
from services.qc.issue_factory import IssueFactory
from services.qc.models import QcIssue, QcMeasured, UnresolvedHumanGate

PRIVACY_DECLARATIONS_SCHEMA: Final = "privacy-declarations-v1"
PRIVACY_GATE_TOOL: Final = "privacy-gate-v1"
_FORBIDDEN_IMPORT_ROOTS: Final = frozenset(
    {"cv2", "face_recognition", "face_detection", "easyocr", "pytesseract", "dlib"}
)
_FORBIDDEN_IMPORT_MARKERS: Final = ("plate", "ocr", "face")
_FORBIDDEN_DEF_PREFIX: Final = "detect_"
_FORBIDDEN_DEF_NAME: Final = "scan_privacy"


class PrivacyScopeError(Exception):
    """Automated privacy detection attempted inside the QC package."""


class PrivacyResolution(StrictModel):
    """One operator resolution record (fixture-marked in tests)."""

    resolved_by: str = Field(min_length=1, strict=True)
    decision: Literal["cleared_for_publication", "removed_from_output", "masked_in_output"]
    record_sha256: Sha256
    fixture_only: bool


class DeclaredPrivacyIssue(StrictModel):
    issue_id: Identifier
    category: Literal["privacy", "rights"]
    declared_by: str = Field(min_length=1, strict=True)
    detail: str = Field(min_length=1, strict=True)
    target_sha256: Sha256 | None = None
    decision_id: str | None = None
    record_span: RecordFrameSpan | None = None
    fixture_only: bool
    resolution: PrivacyResolution | None = None


class PrivacyDeclarations(StrictModel):
    schema_version: Literal["privacy-declarations-v1"]
    declared_issues: tuple[DeclaredPrivacyIssue, ...] = ()

    @classmethod
    def empty(cls) -> PrivacyDeclarations:
        return cls(schema_version=PRIVACY_DECLARATIONS_SCHEMA)


def _import_offense(name: str) -> bool:
    root = name.split(".", 1)[0].lower()
    return root in _FORBIDDEN_IMPORT_ROOTS or any(
        marker in name.lower() for marker in _FORBIDDEN_IMPORT_MARKERS
    )


def _guard_imports(tree: ast.AST) -> list[str]:
    offenses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenses.extend(
                f"import {alias.name}" for alias in node.names if _import_offense(alias.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.module and _import_offense(node.module):
            offenses.append(f"from {node.module} import")
    return offenses


def _guard_definitions(tree: ast.AST) -> list[str]:
    offenses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name.lower()
            if name.startswith(_FORBIDDEN_DEF_PREFIX) or name == _FORBIDDEN_DEF_NAME:
                offenses.append(f"definition {node.name}")
    return offenses


def guard_no_automated_privacy_detectors(source: str, origin: str) -> None:
    """Reject detection imports/definitions in ``source`` (AST-level)."""

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise PrivacyScopeError(f"{origin}: source does not parse: {error}") from error
    offenses = [*_guard_imports(tree), *_guard_definitions(tree)]
    if offenses:
        raise PrivacyScopeError(
            f"{origin}: automated privacy detection is out of scope before Phase 4: "
            + "; ".join(offenses)
        )


def evaluate_privacy_gate(
    declarations: PrivacyDeclarations,
    threshold_version: str,
    inputs: tuple[str, ...],
) -> tuple[tuple[QcIssue, ...], tuple[UnresolvedHumanGate, ...]]:
    """Returns (issues, unresolved_human_gates); pure, never detects."""

    factory = IssueFactory(threshold_version=threshold_version, inputs=inputs)
    issues: list[QcIssue] = []
    gates: list[UnresolvedHumanGate] = []
    for declared in declarations.declared_issues:
        base = (
            QcMeasured(name="category", value=declared.category),
            QcMeasured(name="declared_by", value=declared.declared_by),
        )
        if declared.resolution is None:
            issues.append(
                factory.build(
                    "privacy_rights_unresolved",
                    f"declared {declared.category} issue {declared.issue_id} awaits "
                    "an operator resolution; QC is blocked pending that decision",
                    PRIVACY_GATE_TOOL,
                    base,
                    decision_id=declared.decision_id,
                    record_span=declared.record_span,
                )
            )
            gates.append(
                UnresolvedHumanGate(
                    gate_id=f"privacy-rights-{declared.issue_id}",
                    rule_id="privacy_rights_unresolved",
                    source="privacy_declaration",
                    detail=f"{declared.category} issue {declared.issue_id}: "
                    f"{declared.detail[:200]}",
                )
            )
        else:
            issues.append(
                factory.build(
                    "privacy_rights_resolved",
                    f"declared {declared.category} issue {declared.issue_id} was "
                    f"resolved by operator decision {declared.resolution.decision}",
                    PRIVACY_GATE_TOOL,
                    (
                        *base,
                        QcMeasured(name="decision", value=declared.resolution.decision),
                        QcMeasured(
                            name="record_sha256", value=declared.resolution.record_sha256
                        ),
                    ),
                    decision_id=declared.decision_id,
                    record_span=declared.record_span,
                )
            )
    return tuple(issues), tuple(gates)


__all__ = [
    "PRIVACY_DECLARATIONS_SCHEMA",
    "DeclaredPrivacyIssue",
    "PrivacyDeclarations",
    "PrivacyResolution",
    "PrivacyScopeError",
    "evaluate_privacy_gate",
    "guard_no_automated_privacy_detectors",
]
