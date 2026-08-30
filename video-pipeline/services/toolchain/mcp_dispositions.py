"""Reviewed parity dispositions: loader, zero-gap validator, coverage report.

Task 10 consumes the Task 9 inventory unchanged: every underlying operation
must carry exactly one reviewed disposition row. ``mapped`` rows are proven
statically — the named product surface exists in the adapter's
``SUPPORTED_SURFACES`` authority, or the named typed read API exists on
:class:`~services.mcp_client.ops.McpOps` and passed review as read-only.
``deferred`` rows stay explicit (owner, reason, target phase). Final parity
(strict mode) refuses anything short of 100% mapped — there is no permanent
``refused`` status to hide vendor-supported capabilities behind.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from services.toolchain.mcp_coverage_models import McpInventoryV1, OperationRow
from services.toolchain.mcp_dispositions_models import (
    CoverageReportV1,
    DispositionRow,
    DispositionsError,
    DispositionsParityError,
    DispositionsV1,
)
from services.toolchain.mcp_dispositions_rules import check_row, require
from services.toolchain.mcp_dispositions_static import verify_committed_route_bindings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "read", "mutate", "guarded_mutation", "session_control",
)


def load_inventory(path: Path) -> tuple[McpInventoryV1, str]:
    """Parse ``inventory.json`` (lists→tuples for the strict frozen models).

    Returns the model and the file-bytes SHA-256 the dispositions pin.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        inventory = McpInventoryV1.model_validate(_to_tuples(payload))
    except (OSError, ValueError) as exc:
        raise DispositionsError("inventory-unparsable", str(exc)) from exc
    return inventory, hashlib.sha256(path.read_bytes()).hexdigest()


def _to_tuples(value: object) -> object:
    if isinstance(value, list):
        return tuple(_to_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _to_tuples(item) for key, item in value.items()}
    return value


def load_dispositions(path: Path) -> DispositionsV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DispositionsV1.model_validate(_to_tuples(payload))
    except FileNotFoundError as exc:
        raise DispositionsError("dispositions-missing", str(path)) from exc
    except (OSError, ValueError) as exc:
        raise DispositionsError("dispositions-unparsable", str(exc)) from exc


def dispositions_bytes(dispositions: DispositionsV1) -> bytes:
    payload = json.dumps(
        dispositions.model_dump(exclude_none=True, exclude_defaults=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return payload.encode("utf-8")


def validate_dispositions_files(
    inventory_path: Path,
    dispositions_path: Path,
    *,
    strict: bool = False,
    repo_root: Path | None = None,
) -> CoverageReportV1:
    """Validate the committed artifact pair (inventory bytes are the pin)."""
    inventory, digest = load_inventory(inventory_path)
    return validate_dispositions(
        inventory,
        digest,
        load_dispositions(dispositions_path),
        strict=strict,
        repo_root=repo_root or _REPO_ROOT,
    )


def validate_dispositions(
    inventory: McpInventoryV1,
    inventory_sha256: str,
    dispositions: DispositionsV1,
    *,
    strict: bool,
    repo_root: Path,
) -> CoverageReportV1:
    """Zero-gap validation: pins, one row per operation, static proofs."""
    verify_committed_route_bindings()
    require(
        dispositions.pin_commit == inventory.pin.commit,
        "dispositions-pin-mismatch",
        f"dispositions pin {dispositions.pin_commit} != inventory pin {inventory.pin.commit}",
    )
    require(
        dispositions.inventory_sha256 == inventory_sha256,
        "dispositions-inventory-hash-mismatch",
        f"dispositions pin {dispositions.inventory_sha256} != inventory sha256 {inventory_sha256}",
    )
    ops = {op.operation_id: op for op in inventory.operations}
    rows = {row.operation_id: row for row in dispositions.rows}
    require(
        len(rows) == len(dispositions.rows),
        "dispositions-duplicate-operation",
        f"duplicate operation rows: {', '.join(_duplicates(dispositions.rows))}",
    )
    missing = sorted(set(ops) - set(rows))
    require(
        not missing,
        "dispositions-missing-rows",
        f"{len(missing)} missing, e.g. {missing[:5]}",
    )
    unknown = sorted(set(rows) - set(ops))
    require(not unknown, "dispositions-unknown-operations", f"unknown rows: {unknown[:5]}")
    for row in dispositions.rows:
        check_row(row, ops[row.operation_id], rows, ops, repo_root)
    return _report(dispositions, ops, strict=strict)


def _report(
    dispositions: DispositionsV1, ops: dict[str, OperationRow], *, strict: bool
) -> CoverageReportV1:
    total = len(dispositions.rows)
    mapped = sum(1 for row in dispositions.rows if row.status == "mapped")
    deferred_rows = [row for row in dispositions.rows if row.status == "deferred"]
    by_category: dict[str, int] = dict.fromkeys(_CATEGORY_ORDER, 0)
    by_domain: dict[str, int] = {}
    for row in deferred_rows:
        by_category[row.category] += 1
        domain = ops[row.operation_id].tool
        by_domain[domain] = by_domain.get(domain, 0) + 1
    coverage = mapped / total if total else 0.0
    if strict:
        if deferred_rows:
            raise DispositionsParityError(
                "parity-deferred-remaining",
                f"{len(deferred_rows)} deferred rows remain; by domain: "
                + json.dumps(dict(sorted(by_domain.items())), sort_keys=True),
            )
        if coverage != 1.0:
            raise DispositionsParityError(
                "parity-coverage-incomplete", f"coverage {coverage:.4f} < 1.0"
            )
    return CoverageReportV1(
        mode="final" if strict else "rollout",
        total_operations=total,
        mapped=mapped,
        deferred=len(deferred_rows),
        unmapped=0,
        refused_vendor_supported=0,
        coverage=coverage,
        deferred_by_category=by_category,
        deferred_by_domain=dict(sorted(by_domain.items())),
    )


def _duplicates(rows: tuple[DispositionRow, ...]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for row in rows:
        if row.operation_id in seen:
            dupes.append(row.operation_id)
        seen.add(row.operation_id)
    return dupes


__all__ = [
    "DispositionsError",
    "DispositionsParityError",
    "DispositionsV1",
    "dispositions_bytes",
    "load_dispositions",
    "load_inventory",
    "validate_dispositions",
    "validate_dispositions_files",
]
