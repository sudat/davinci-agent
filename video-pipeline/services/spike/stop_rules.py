"""Phase-0A Stop Gate rules and the cross-gate stop marker.

The two Stop criteria from the frozen policy are evaluated from raw evidence:
source identity/frame verifiability and MVP-capability availability. A
triggered Stop writes ``gate-stop-marker.json`` next to the evidence
directory (shared by every later phase gate) and every gate run refuses to
start while that marker is present.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.spike.gate_models import (
    GATE_ID,
    STOP_NAME,
    CapabilityEntry,
    CapabilityMatrix,
    EvidenceRef,
    GateStopRecord,
)

STOP_IDENTITY: Final = "phase-0a-stop-source-identity-or-frame-unverifiable"
STOP_CAPABILITY: Final = "phase-0a-stop-mvp-capability-unavailable"


def stop_marker_path(evidence_dir: Path) -> Path:
    return evidence_dir.parent / STOP_NAME


def stop_recorded(evidence_dir: Path) -> GateStopRecord | None:
    """Return the recorded stop for this evidence tree, if any (fail-closed)."""

    marker = stop_marker_path(evidence_dir)
    if not marker.is_file():
        return None
    return load_stop(marker)


def record_stop(evidence_dir: Path, criterion_id: str, reason: str) -> Path:
    marker = stop_marker_path(evidence_dir)
    record = GateStopRecord(
        gate_id=GATE_ID,
        criterion_id=criterion_id,
        reason=reason,
        recorded_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    atomic_write(marker, canonical_model_bytes(record))
    return marker


def load_stop(marker: Path) -> GateStopRecord:
    """Load a stop marker; an unreadable marker is treated as a recorded stop."""

    try:
        return GateStopRecord.model_validate_json(marker.read_bytes())
    except (OSError, ValueError):
        return GateStopRecord(
            gate_id=GATE_ID,
            criterion_id=STOP_IDENTITY,
            reason=f"stop marker present but unreadable: {marker}",
            recorded_at="unknown",
        )


def identity_stop_reason(
    host_report_hash: str | None,
    run_host_hashes: tuple[str, ...],
    expected_fingerprint: str | None,
) -> str | None:
    """Return the Stop reason when source identity or frames are unverifiable."""

    if host_report_hash is None:
        return "host report unreadable: live source identity cannot be verified"
    drifted = sorted(
        f"run[{index}]"
        for index, bound in enumerate(run_host_hashes, start=1)
        if bound != host_report_hash
    )
    if drifted:
        return (
            "build evidence bound to a different host report than the current one: "
            + ",".join(drifted)
        )
    if expected_fingerprint is None:
        return (
            "expected timeline fingerprint not derivable from manifest+fixture: "
            "frames unverifiable"
        )
    return None


def _refs_hash_match(refs: tuple[EvidenceRef, ...], evidence_dir: Path) -> bool:
    for ref in refs:
        path = evidence_dir / ref.path
        try:
            if sha256_file(path) != ref.sha256:
                return False
        except OSError:
            return False
    return True


def _entry_stop_reason(
    capability: str,
    entry: CapabilityEntry | None,
    evidence_dir: Path | None,
) -> str | None:
    if entry is None:
        return f"required capability absent from the matrix: {capability}"
    if not entry.api_available:
        return f"required capability has no official/interchange path: {capability}"
    if not entry.live_verified:
        return f"required capability not live-verified: {capability}"
    if evidence_dir is not None and not _refs_hash_match(entry.evidence_refs, evidence_dir):
        return f"capability evidence refs do not hash-match the evidence dir: {capability}"
    return None


def capability_stop_reason(
    matrix: CapabilityMatrix | None,
    evidence_dir: Path | None = None,
) -> str | None:
    """Return the Stop reason when an MVP capability lacks a verified path.

    ``evidence_dir`` additionally enforces that every referenced evidence
    file still hashes to the recorded value; omit it for a semantic-only
    check of a stored matrix.
    """

    if matrix is None:
        return "capability matrix missing: MVP capability availability unverifiable"
    if matrix.schema_version != "capability-matrix-v1" or not matrix.capabilities:
        return "capability matrix malformed: MVP capability availability unverifiable"
    by_id = {entry.capability: entry for entry in matrix.capabilities}
    for capability in PHASE_0A_CAPABILITIES:
        reason = _entry_stop_reason(capability, by_id.get(capability), evidence_dir)
        if reason is not None:
            return reason
    return None


def capability_stop_reason_semantic(matrix: CapabilityMatrix | None) -> str | None:
    """Stored-matrix Stop check without evidence-ref file hashing."""

    return capability_stop_reason(matrix, None)
