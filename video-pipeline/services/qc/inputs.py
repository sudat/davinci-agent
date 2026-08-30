"""Bound-input loading and capability-matrix verification for the QC CLI.

Every bound artifact parses through a lax TypeAdapter (canonical JSON stores
tuples as arrays, which strict models refuse) while every field constraint
still applies; the policy hash is verified before anything runs; required
capabilities are checked against the hash-bound capability matrix and fail
closed when unsupported or absent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from services.contracts.timeline_ir import TimelineIrProduction
from services.foundation_io import sha256_file
from services.ingest.models import SourceManifest
from services.preview.models import PreviewTraceManifest
from services.qc.issue_factory import IssueFactory
from services.qc.models import (
    CapabilityRecord,
    QcInputBinding,
    QcIssue,
    QcMeasured,
    QcPolicy,
)
from services.qc.privacy_gate import PrivacyDeclarations


class QcInputError(Exception):
    """A bound input is missing, unreadable, or malformed."""


@dataclass(frozen=True, slots=True)
class OptionalBindings:
    ir: Path | None = None
    preview: Path | None = None
    analysis: Path | None = None
    privacy: Path | None = None
    source_manifest: Path | None = None
    edit_source: Path | None = None


@dataclass(frozen=True, slots=True)
class LoadedInputs:
    policy: QcPolicy
    ir: TimelineIrProduction | None = None
    preview: PreviewTraceManifest | None = None
    declarations: PrivacyDeclarations = field(default_factory=PrivacyDeclarations.empty)
    source_manifest: SourceManifest | None = None
    edit_source: Path | None = None
    bindings: tuple[QcInputBinding, ...] = ()


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise QcInputError(f"cannot read {path}: {error}") from error


def _parse_or_raise[ParsedModel](
    raw: bytes,
    what: str,
    path: Path,
    model: type[ParsedModel],
) -> ParsedModel:
    try:
        return TypeAdapter(model).validate_json(raw, strict=False)
    except ValidationError as error:
        raise QcInputError(f"{what} binding malformed ({path}): {error}") from error


def _orientation_bindings(
    extra: OptionalBindings, bindings: list[QcInputBinding]
) -> tuple[SourceManifest | None, Path | None]:
    """Parse + hash-bind the T9 orientation inputs (Source Manifest + edit
    source); each is optional and absent means no orientation claim."""
    manifest: SourceManifest | None = None
    if extra.source_manifest is not None:
        manifest = _parse_or_raise(
            _read(extra.source_manifest),
            "source-manifest",
            extra.source_manifest,
            SourceManifest,
        )
        bindings.append(
            QcInputBinding(
                kind="source_manifest", sha256=sha256_file(extra.source_manifest)
            )
        )
    edit_source: Path | None = None
    if extra.edit_source is not None:
        if not extra.edit_source.is_file():
            raise QcInputError(f"edit-source binding missing: {extra.edit_source}")
        edit_source = extra.edit_source
        bindings.append(
            QcInputBinding(kind="edit_source", sha256=sha256_file(extra.edit_source))
        )
    return manifest, edit_source


def load_inputs(render: Path, policy_path: Path, extra: OptionalBindings) -> LoadedInputs:
    policy_raw = _read(policy_path)
    try:
        policy = QcPolicy.model_validate_json(policy_raw)
    except ValidationError as error:
        raise QcInputError(f"policy malformed: {error}") from error
    if not policy.verify_hash():
        raise QcInputError("policy content hash does not match policy_sha256")
    bindings: list[QcInputBinding] = [
        QcInputBinding(kind="render", sha256=sha256_file(render)),
        QcInputBinding(
            kind="policy", sha256=hashlib.sha256(policy_raw).hexdigest()
        ),
    ]
    ir: TimelineIrProduction | None = None
    if extra.ir is not None:
        ir = _parse_or_raise(_read(extra.ir), "IR", extra.ir, TimelineIrProduction)
        bindings.append(QcInputBinding(kind="ir", sha256=sha256_file(extra.ir)))
    preview: PreviewTraceManifest | None = None
    if extra.preview is not None:
        preview = _parse_or_raise(
            _read(extra.preview), "preview", extra.preview, PreviewTraceManifest
        )
        bindings.append(
            QcInputBinding(kind="preview", sha256=sha256_file(extra.preview))
        )
    if extra.analysis is not None:
        if not extra.analysis.is_file():
            raise QcInputError(f"analysis binding missing: {extra.analysis}")
        bindings.append(
            QcInputBinding(kind="analysis", sha256=sha256_file(extra.analysis))
        )
    declarations = PrivacyDeclarations.empty()
    if extra.privacy is not None:
        declarations = _parse_or_raise(
            _read(extra.privacy), "privacy", extra.privacy, PrivacyDeclarations
        )
        bindings.append(
            QcInputBinding(kind="privacy", sha256=sha256_file(extra.privacy))
        )
    source_manifest, edit_source = _orientation_bindings(extra, bindings)
    return LoadedInputs(
        policy=policy,
        ir=ir,
        preview=preview,
        declarations=declarations,
        source_manifest=source_manifest,
        edit_source=edit_source,
        bindings=tuple(bindings),
    )


def _matrix_records(policy: QcPolicy) -> dict[str, CapabilityRecord]:
    if policy.capability_matrix is None:
        return {}
    matrix_path = Path(policy.capability_matrix.path)
    try:
        raw = json.loads(_read(matrix_path))
        if sha256_file(matrix_path) != policy.capability_matrix.sha256:
            raise QcInputError(
                f"capability matrix hash drift: {matrix_path} no longer matches "
                "the policy binding"
            )
        return {
            entry["capability"]: CapabilityRecord.model_validate(
                {
                    "capability": entry["capability"],
                    "api_available": entry["api_available"],
                    "live_verified": entry["live_verified"],
                }
            )
            for entry in raw["capabilities"]
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
        raise QcInputError(f"capability matrix unreadable: {error}") from error


def capability_issues(
    policy: QcPolicy, inputs: tuple[str, ...]
) -> tuple[QcIssue, ...]:
    """Fail closed: a required capability absent or unverified blocks QC."""

    if not policy.required_capabilities:
        return ()
    factory = IssueFactory.for_policy(policy, inputs)
    records = _matrix_records(policy)
    issues: list[QcIssue] = []
    for required in policy.required_capabilities:
        record = records.get(required)
        if record is None or not record.api_available or not record.live_verified:
            observed = (
                "absent-from-matrix"
                if record is None
                else f"api_available={record.api_available} "
                f"live_verified={record.live_verified}"
            )
            issues.append(
                factory.build(
                    "qc_capability_unsupported",
                    f"required capability {required} is unsupported: {observed}; "
                    "QC cannot verify and fails closed",
                    "capability-matrix-v1",
                    (QcMeasured(name="capability", value=required),),
                )
            )
    return tuple(sorted(issues, key=lambda i: (i.rule_id, i.detail)))


__all__ = [
    "LoadedInputs",
    "OptionalBindings",
    "QcInputError",
    "capability_issues",
    "load_inputs",
]
