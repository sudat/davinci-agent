"""Capability-matrix derivation for the Phase-0B gate (evidence-ref-bound).

Derives the new 0B capability/findings from the recomputed readback reports —
every ``live_verified`` flag comes from raw evidence hashes, never an authored
flag — writes the 0B matrix into the evidence tree, and APPENDS only new
findings to the published ``capabilities/resolve-<version>/capability-matrix.json``
(0A entries are never rewritten; appends are idempotent by finding id).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates.phase0b import PHASE_0B_VARIANTS
from services.spike.gate_models import (
    ApiFindingEntry,
    CapabilityEntry,
    CapabilityMatrix,
    EvidenceRef,
)
from services.spike.gate_phase0b_models import (
    MATRIX0B_NAME,
    READBACK_REPORT_NAME,
    LiveReadbackReport,
    readback_report_path,
)

MATRIX_SCHEMA: Final = "capability-matrix-v1"


def _report_ref(evidence: Path, variant: str) -> EvidenceRef:
    path = readback_report_path(evidence, variant)
    return EvidenceRef(
        path=path.relative_to(evidence).as_posix(), sha256=sha256_file(path)
    )


def derive0b_matrix(evidence: Path, resolve_version: str, resolve_build: str) -> CapabilityMatrix:
    reports = {
        variant: readback_report_path(evidence, variant) for variant in PHASE_0B_VARIANTS
    }
    missing = [variant for variant, path in reports.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"readback reports missing: {missing}")
    all_refs = tuple(_report_ref(evidence, variant) for variant in PHASE_0B_VARIANTS)
    deltas = {
        variant: _max_delta(reports[variant].parent) for variant in PHASE_0B_VARIANTS
    }
    ntsc_refs = (
        _report_ref(evidence, "p0b-ntsc2997"),
        _report_ref(evidence, "p0b-ntsc5994"),
    )
    rotate_ref = (_report_ref(evidence, "p0b-rotate90"),)
    capabilities = (
        CapabilityEntry(
            capability="edit_source_readback",
            api_available=True,
            live_verified=all(delta == 0 for delta in deltas.values()),
            evidence_refs=all_refs,
            limitations=(
                "normalized CFR30 edit mezzanines only; exact spans read as "
                "GetSourceStartFrame()+GetDuration(); originals are refused by the "
                "edit-source guard before any Resolve call"
            ),
        ),
    )
    findings: tuple[ApiFindingEntry, ...] = (
        _finding(
            "edit-source-anchor-frame-readback",
            live_verified=all(delta == 0 for delta in deltas.values()),
            refs=all_refs,
            limitations=(
                "AppendToTimeline sub-clips at arbitrary edit-source frames read back exact "
                "integer source/record frames for all six 0B variants (frame delta 0)"
            ),
        ),
        _finding(
            "rational-rate-normalized-import",
            live_verified=deltas["p0b-ntsc2997"] == 0 and deltas["p0b-ntsc5994"] == 0,
            refs=ntsc_refs,
            limitations=(
                "30000/1001- and 60000/1001-derived edit sources import and read back "
                "exact frames after CFR30 normalization"
            ),
        ),
        _finding(
            "rotate90-edit-source-import",
            live_verified=deltas["p0b-rotate90"] == 0,
            refs=rotate_ref,
            limitations=(
                "tkhd-rotate90 original normalized with -noautorotate imports as an edit "
                "source with exact frame readback; clip properties recorded in the report"
            ),
        ),
    )
    return CapabilityMatrix(
        schema_version=MATRIX_SCHEMA,
        resolve_version=resolve_version,
        resolve_build=resolve_build,
        capabilities=capabilities,
        findings=findings,
    )


def _finding(
    finding: str,
    *,
    live_verified: bool,
    refs: tuple[EvidenceRef, ...],
    limitations: str,
) -> ApiFindingEntry:
    return ApiFindingEntry(
        finding=finding,
        api_available=True,
        live_verified=live_verified,
        evidence_refs=refs,
        limitations=limitations,
    )


def _max_delta(readback_dir: Path) -> int:
    report = LiveReadbackReport.model_validate_json(
        (readback_dir / READBACK_REPORT_NAME).read_bytes()
    )
    return report.max_frame_delta


def write0b_matrix(
    matrix: CapabilityMatrix, evidence: Path, capabilities_dir: Path | None
) -> list[Path]:
    written = [evidence / MATRIX0B_NAME]
    atomic_write(evidence / MATRIX0B_NAME, canonical_model_bytes(matrix))
    if capabilities_dir is None:
        return written
    published = capabilities_dir / f"resolve-{matrix.resolve_version}" / "capability-matrix.json"
    if published.is_file():
        existing = CapabilityMatrix.model_validate_json(published.read_bytes())
        known = {finding.finding for finding in existing.findings}
        fresh = tuple(f for f in matrix.findings if f.finding not in known)
        if not fresh:
            return [*written, published]
        merged = existing.model_copy(
            update={"findings": (*existing.findings, *fresh)}
        )
        atomic_write(published, canonical_model_bytes(merged))
        written.append(published)
        return written
    published.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(published, canonical_model_bytes(matrix))
    written.append(published)
    return written
