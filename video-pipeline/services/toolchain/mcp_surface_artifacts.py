"""Light loaders + agreement seal for the committed MCP coverage artifacts.

Task 11. Parses the Task 9 inventory with its own strict model, reads the
Task 10 dispositions SEAL fields only, and verifies the manifest-v1 seal —
so a pin upgrade must regenerate inventory, dispositions, and manifest in
one change set or loading fails typed.

This module deliberately avoids :mod:`services.toolchain.mcp_dispositions`:
that package imports the execution stack, which would cycle back into the
MCP client (the constraint Task 11 calls out). Full row validation stays
Task 10's CI concern; here only the artifact agreement is enforced.

Every error detail carries safe identifiers only — no absolute paths.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_client.errors import McpSurfaceConfigError
from services.release.manifest import MANIFEST_NAME, Manifest
from services.toolchain.mcp_coverage_models import McpInventoryV1

if TYPE_CHECKING:
    from services.toolchain.mcp_pin import McpPin

INVENTORY_NAME: Final = "inventory.json"
DISPOSITIONS_NAME: Final = "dispositions.json"
_HEX: Final = frozenset("0123456789abcdef")
_SHA256_HEX_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class SurfaceFacts:
    """The committed baseline facts extracted from the artifact triple."""

    pin_commit: str
    inventory_sha256: str
    provider_version: str
    tool_schema_sha256: dict[str, str]
    tool_actions: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _DispositionsSeal:
    pin_commit: str
    inventory_sha256: str


def load_surface_facts(*, pin: McpPin, coverage_dir: Path) -> SurfaceFacts:
    """Load and cross-validate the artifact triple; stale inputs fail typed.

    Callers hand the ALREADY-PARSED pin (a pin-backed transport config owns
    one), so the pin file is parsed exactly once per construction.
    """
    inventory, inventory_digest = _load_inventory(coverage_dir / INVENTORY_NAME)
    seal, dispositions_digest = _load_dispositions_seal(coverage_dir / DISPOSITIONS_NAME)
    _check_manifest(
        coverage_dir,
        inventory_digest=inventory_digest,
        dispositions_digest=dispositions_digest,
    )
    commits = {
        "pin": pin.commit,
        "inventory": inventory.pin.commit,
        "dispositions": seal.pin_commit,
    }
    if len(set(commits.values())) != 1:
        raise McpSurfaceConfigError(
            "surface-pin-commit-mismatch",
            f"pin/inventory/dispositions commits disagree: {commits}",
        )
    if seal.inventory_sha256 != inventory_digest:
        raise McpSurfaceConfigError(
            "surface-inventory-hash-stale",
            f"dispositions pins inventory {seal.inventory_sha256[:12]},"
            f" file bytes hash {inventory_digest[:12]}",
        )
    return SurfaceFacts(
        pin_commit=pin.commit,
        inventory_sha256=inventory_digest,
        provider_version=inventory.pin.provider_version,
        tool_schema_sha256={
            row.name: row.input_schema_sha256 for row in inventory.compound_tools
        },
        tool_actions={
            row.name: tuple(sorted(row.actions)) for row in inventory.compound_tools
        },
    )


def _load_inventory(path: Path) -> tuple[McpInventoryV1, str]:
    """Parse with the Task 9 strict model; digest is over the file bytes."""
    if not path.is_file():
        raise McpSurfaceConfigError(
            "surface-inventory-missing", "inventory.json is absent from the coverage directory"
        )
    data = path.read_bytes()
    try:
        payload: object = json.loads(data)
        inventory = McpInventoryV1.model_validate(_to_tuples(payload))
    except (OSError, ValueError) as exc:
        raise McpSurfaceConfigError(
            "surface-inventory-unparsable",
            "inventory.json is not a valid mcp-inventory-v1 document",
        ) from exc
    return inventory, hashlib.sha256(data).hexdigest()


def _load_dispositions_seal(path: Path) -> tuple[_DispositionsSeal, str]:
    """Read only the seal fields (see module docstring for why not the full model)."""
    if not path.is_file():
        raise McpSurfaceConfigError(
            "surface-dispositions-missing",
            "dispositions.json is absent from the coverage directory",
        )
    data = path.read_bytes()
    try:
        payload: object = json.loads(data)
    except (OSError, ValueError) as exc:
        raise McpSurfaceConfigError(
            "surface-dispositions-unparsable", "dispositions.json is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise McpSurfaceConfigError(
            "surface-dispositions-unparsable", "dispositions.json is not a JSON object"
        )
    pin_commit = payload.get("pin_commit")
    inventory_sha256 = payload.get("inventory_sha256")
    if not isinstance(pin_commit, str) or not isinstance(inventory_sha256, str):
        raise McpSurfaceConfigError(
            "surface-dispositions-unparsable",
            "dispositions.json lacks string pin_commit/inventory_sha256 seal fields",
        )
    if set(inventory_sha256) > _HEX or len(inventory_sha256) != _SHA256_HEX_LENGTH:
        raise McpSurfaceConfigError(
            "surface-dispositions-unparsable",
            "dispositions.json inventory_sha256 is not a sha256 hex digest",
        )
    return (
        _DispositionsSeal(pin_commit=pin_commit, inventory_sha256=inventory_sha256),
        hashlib.sha256(data).hexdigest(),
    )


def _check_manifest(
    coverage_dir: Path, *, inventory_digest: str, dispositions_digest: str
) -> None:
    path = coverage_dir / MANIFEST_NAME
    if not path.is_file():
        raise McpSurfaceConfigError(
            "surface-manifest-missing", "manifest.json is absent from the coverage directory"
        )
    try:
        manifest = Manifest.model_validate(
            _to_tuples(json.loads(path.read_text(encoding="utf-8")))
        )
    except (OSError, ValueError) as exc:
        raise McpSurfaceConfigError(
            "surface-manifest-unparsable", "manifest.json is not a valid manifest-v1 document"
        ) from exc
    entries = {entry.path: entry.sha256 for entry in manifest.entries}
    if set(entries) != {INVENTORY_NAME, DISPOSITIONS_NAME}:
        raise McpSurfaceConfigError(
            "surface-manifest-entry-set",
            f"manifest entries {sorted(entries)} are not exactly"
            f" [{DISPOSITIONS_NAME}, {INVENTORY_NAME}]",
        )
    expected = {INVENTORY_NAME: inventory_digest, DISPOSITIONS_NAME: dispositions_digest}
    mismatched = sorted(name for name, digest in expected.items() if entries[name] != digest)
    if mismatched:
        raise McpSurfaceConfigError(
            "surface-manifest-mismatch",
            f"manifest digest does not match file bytes for {mismatched}",
        )


def _to_tuples(value: object) -> object:
    """JSON lists → tuples for the strict frozen models (mcp_dispositions parity)."""
    if isinstance(value, list):
        return tuple(_to_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _to_tuples(item) for key, item in value.items()}
    return value


__all__ = [
    "DISPOSITIONS_NAME",
    "INVENTORY_NAME",
    "SurfaceFacts",
    "load_surface_facts",
]
