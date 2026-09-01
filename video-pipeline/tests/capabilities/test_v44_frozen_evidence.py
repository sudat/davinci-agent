"""Frozen v4.4 evidence fingerprints (append-only history guard).

These files are closed measurement records (PRD v4.4 §6.7.2-§6.7.3 and the
2026-09-01 measurement-policy predeclaration, plan
``.omo/plans/v44-asr-measurement-policy-v2.md``). Any change to one of them
must arrive as a NEW dated file, never as an edit to these bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

CAPABILITIES_V44: Final = (
    Path(__file__).resolve().parents[2] / "capabilities" / "v4.4"
)

#: path (relative to capabilities/v4.4) -> pinned sha256. Values for the
#: resolve-auto-caption trio are the ones recorded in PRD §6.7.3.
FROZEN_SHA256: Final[dict[str, str]] = {
    "product-proof/v44-0/SUMMARY.txt": (
        "2c28cb67f81a00f810c400dd9132140899dbf6d613c162b1be2b2b7c75c5a79a"
    ),
    "product-proof/v44-0/r4-diagnostic-summary.json": (
        "545b6b8f65b88f0281528b638b2e2b4b8429ae039cf1c5e84ca573da4082a787"
    ),
    "product-proof/v44-0/truth-freeze.json": (
        "e31c817e88e99f796e8a0712bc36c054587c71cf537a15e1bc2897bb42fe9773"
    ),
    "probes/resolve-auto-caption/summary.json": (
        "e737a982b5e957da33cf8a5f8cd08cdd9b09ca5e53f798bf01363a82275d4538"
    ),
    "probes/resolve-auto-caption/capability-r1.json": (
        "f04415632aa9ffd5ab77594a4c9fd6c952dbe83ec9fb013038cb4433a5351450"
    ),
    "probes/resolve-auto-caption/capability-r2.json": (
        "08c1654c750783d2605a2b1357b21663f20165cbca922a50aaea1f6c7ea9413c"
    ),
    "probes/resolve-auto-caption/quality-metrics.json": (
        "fb06cd1d794113c76fd3f569c5c927eb1a92ba7df605f78e9146e1a4f4e9a6ca"
    ),
}


def test_frozen_v44_evidence_files_are_byte_identical() -> None:
    """Given: the frozen v4.4 evidence set; When: each file is re-hashed;
    Then: every sha256 equals its pinned constant — history stays append-only
    across the measurement-policy v2 work and every later wave."""
    for relative, pinned in FROZEN_SHA256.items():
        path = CAPABILITIES_V44 / relative
        assert path.is_file(), f"frozen evidence missing: {relative}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == pinned, f"frozen evidence drifted: {relative}"
