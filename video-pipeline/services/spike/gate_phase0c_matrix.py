"""Capability-matrix policy for the Phase-0C gate (evidence-ref-bound appends).

The 0C gate exercises the Resolve-free pinned-ffmpeg preview path; it freezes
no Resolve capability allowlist (policy ``capability_allowlist`` is empty) and
normally surfaces NO new capability findings — the preview/review behaviors
were already published by their owning todos. This module therefore derives
an empty finding set on the happy path and APPENDS only genuinely new,
evidence-ref-bound findings (idempotently by finding id) should the gate ever
surface one (for example a new ffmpeg/preview behavior).
"""

from __future__ import annotations

from pathlib import Path

from services.foundation_io import atomic_write, canonical_model_bytes
from services.spike.gate_models import ApiFindingEntry, CapabilityMatrix

MATRIX_SCHEMA = "capability-matrix-v1"


def derive0c_findings(evidence: Path) -> tuple[ApiFindingEntry, ...]:
    """Derive new 0C capability findings from raw gate evidence.

    Currently empty by design: the gate binds all four criteria to the frozen
    preview/review contracts and publishes no new capability claims. Any new
    ffmpeg/preview behavior observed here must be appended with evidence refs
    bound to files under ``evidence``.
    """

    del evidence
    return ()


def append0c_findings(
    findings: tuple[ApiFindingEntry, ...],
    capabilities_dir: Path,
    resolve_version: str,
) -> list[Path]:
    """Idempotently append new findings to the published capability matrix."""

    written: list[Path] = []
    if not findings:
        return written
    published = capabilities_dir / f"resolve-{resolve_version}" / "capability-matrix.json"
    if not published.is_file():
        return written
    existing = CapabilityMatrix.model_validate_json(published.read_bytes())
    known = {finding.finding for finding in existing.findings}
    fresh = tuple(finding for finding in findings if finding.finding not in known)
    if not fresh:
        return written
    merged = existing.model_copy(update={"findings": (*existing.findings, *fresh)})
    atomic_write(published, canonical_model_bytes(merged))
    written.append(published)
    return written


__all__ = [
    "append0c_findings",
    "derive0c_findings",
]
