"""Resolve-package toolchain section for Phase 2 (capability-matrix binding).

The pin freezes the binding between Phase-2 package compilation and the
Todo-19 frozen live-verified capability matrix: exact matrix path + sha256,
Resolve version, and the five required capability ids. The smoke is fully
deterministic and offline: the matrix bytes hash to the pinned value, the
version bindings agree, and every required capability row is
``api_available`` + ``live_verified`` with non-empty evidence references.
No Resolve launch, no network, no fabricated live result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write

REQUIRED_CAPABILITY_IDS: tuple[str, ...] = (
    "base_cut",
    "fixed_subtitle",
    "media_intro_outro",
    "basic_audio_preset",
    "render",
)

RequiredCapability = Literal[
    "base_cut",
    "fixed_subtitle",
    "media_intro_outro",
    "basic_audio_preset",
    "render",
]


class ResolvePackageSection(StrictModel):
    schema_version: Literal["resolve-package-pin-v1"]
    capability_matrix_path: str
    capability_matrix_sha256: Sha256
    resolve_version: Literal["21.0.4"]
    resolve_build: Literal["21.0.40005"]
    required_capabilities: tuple[RequiredCapability, ...] = Field(min_length=1)
    external_credentials: Literal["none"]


class ResolvePackageSmokeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def load_capability_matrix(section: ResolvePackageSection, root: Path) -> dict[str, object]:
    """Load and structurally validate the pinned capability matrix (offline)."""

    raw = (root / section.capability_matrix_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != section.capability_matrix_sha256:
        raise ResolvePackageSmokeError(
            f"capability matrix hash drift: {section.capability_matrix_path}"
        )
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        raise ResolvePackageSmokeError("capability matrix is not an object")
    if payload.get("schema_version") != "capability-matrix-v1":
        raise ResolvePackageSmokeError("capability matrix schema version drift")
    if payload.get("resolve_version") != section.resolve_version:
        raise ResolvePackageSmokeError("capability matrix resolve version drift")
    if payload.get("resolve_build") != section.resolve_build:
        raise ResolvePackageSmokeError("capability matrix resolve build drift")
    return payload


def _capability_rows(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list):
        raise ResolvePackageSmokeError("capability matrix has no capabilities table")
    rows: dict[str, dict[str, object]] = {}
    for row in capabilities:
        if isinstance(row, dict) and isinstance(row.get("capability"), str):
            rows[str(row["capability"])] = row
    return rows


def verify_resolve_package_contract(
    section: ResolvePackageSection, root: Path
) -> dict[str, object]:
    if section.external_credentials != "none":
        raise ResolvePackageSmokeError("resolve-package pin must carry no credentials")
    payload = load_capability_matrix(section, root)
    rows = _capability_rows(payload)
    for required in section.required_capabilities:
        row = rows.get(required)
        if row is None:
            raise ResolvePackageSmokeError(f"required capability missing: {required}")
        if row.get("api_available") is not True:
            raise ResolvePackageSmokeError(f"capability not api-available: {required}")
        if row.get("live_verified") is not True:
            raise ResolvePackageSmokeError(f"capability not live-verified: {required}")
        evidence = row.get("evidence_refs")
        if not isinstance(evidence, list) or not evidence:
            raise ResolvePackageSmokeError(f"capability has no matrix evidence: {required}")
    return payload


def _evidence_count(row: dict[str, object]) -> int:
    refs = row.get("evidence_refs")
    return len(refs) if isinstance(refs, list) else 0


def run_resolve_package_smoke(
    section: ResolvePackageSection, root: Path, smoke_dir: Path
) -> Path:
    """Deterministic offline matrix-bound validation; evidence under smoke_dir."""

    smoke_dir.mkdir(parents=True, exist_ok=True)
    payload = verify_resolve_package_contract(section, root)
    rows = _capability_rows(payload)
    evidence_payload = {
        "capability_matrix_path": section.capability_matrix_path,
        "capability_matrix_sha256": section.capability_matrix_sha256,
        "external_credentials": section.external_credentials,
        "live_rerun": False,
        "required_capabilities": {
            required: {
                "api_available": rows[required].get("api_available") is True,
                "evidence_refs": _evidence_count(rows[required]),
                "live_verified": rows[required].get("live_verified") is True,
            }
            for required in section.required_capabilities
        },
        "resolve_build": section.resolve_build,
        "resolve_version": section.resolve_version,
    }
    evidence = smoke_dir / "resolve-package-smoke.json"
    atomic_write(
        evidence,
        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode(),
    )
    return evidence
