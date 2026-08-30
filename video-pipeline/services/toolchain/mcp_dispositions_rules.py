"""Per-row disposition validation rules (alias, route, risk, evidence).

Split from :mod:`services.toolchain.mcp_dispositions` to stay under the
module LOC ceiling; the orchestrator there drives these checks over every
row. Every failure is a typed :class:`DispositionsError` with a stable code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_client.ops import McpOps
from services.mcp_execution.live_adapter import SUPPORTED_SURFACES
from services.toolchain.mcp_dispositions_bindings import (
    OPS_READ_OPERATION_BINDINGS,
    SURFACE_OPERATION_BINDINGS,
)
from services.toolchain.mcp_dispositions_models import (
    OPS_READ_METHODS,
    PLANNED_ROUTE_TARGETS,
    READ_ACTION_EXCEPTIONS,
    READ_ACTION_RE,
    READ_ACTION_SUFFIXES,
    SESSION_CONTROL_OPERATIONS,
    DispositionRow,
    DispositionsError,
)

if TYPE_CHECKING:
    from services.toolchain.mcp_coverage_models import OperationRow

_VENDOR_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^vendor:[A-Za-z0-9_./-]+:[A-Za-z0-9_.-]+$"
)
_DESTRUCTIVE_SAFETY: Final[frozenset[str]] = frozenset(
    ("destructive", "external_destructive")
)


def check_row(
    row: DispositionRow,
    op: OperationRow,
    rows: dict[str, DispositionRow],
    ops: dict[str, OperationRow],
    repo_root: Path,
) -> None:
    target = None
    if row.alias_of is not None:
        target = check_alias(row, row.alias_of, op, rows, ops)
    if row.status == "deferred":
        for field in ("owner", "reason", "target_phase"):
            require(
                getattr(row, field).strip(),
                "dispositions-deferred-metadata-missing",
                f"{row.operation_id}: empty {field}",
            )
        require(
            row.route.kind == "planned" and row.route.target in PLANNED_ROUTE_TARGETS,
            "dispositions-route-invalid",
            f"{row.operation_id}: deferred route must be one of {sorted(PLANNED_ROUTE_TARGETS)}",
        )
    else:
        check_mapped_route(row, row.alias_of or row.operation_id)
    # An alias row IS the target's underlying operation: risk constraints are
    # evaluated against the compound target's inventory facts (its category,
    # status, and route are inherited from there by the reviewed mapping).
    check_risk(row, ops[row.alias_of] if row.alias_of else op, target)
    check_evidence(row, repo_root, needs_repo_evidence=row.status == "mapped")


def check_alias(
    row: DispositionRow,
    alias_of: str,
    op: OperationRow,
    rows: dict[str, DispositionRow],
    ops: dict[str, OperationRow],
) -> DispositionRow:
    require(
        op.source == "granular_only",
        "dispositions-alias-source-invalid",
        f"{row.operation_id}: only granular-only operations may alias",
    )
    require(
        alias_of != row.operation_id and alias_of in rows,
        "dispositions-alias-target-invalid",
        f"{row.operation_id}: alias target {alias_of!r} is not a disposition row",
    )
    target = rows[alias_of]
    require(
        target.alias_of is None,
        "dispositions-alias-chain",
        f"{row.operation_id}: alias target {alias_of!r} is itself an alias",
    )
    require(
        ops[alias_of].source == "compound",
        "dispositions-alias-target-invalid",
        f"{row.operation_id}: alias target {alias_of!r} must be a compound operation",
    )
    for field in ("category", "status", "route"):
        require(
            getattr(row, field) == getattr(target, field),
            "dispositions-alias-inheritance-mismatch",
            f"{row.operation_id}: {field} must inherit {alias_of!r}",
        )
    require(
        any(ref.startswith("vendor:") for ref in row.evidence),
        "dispositions-alias-evidence-missing",
        f"{row.operation_id}: alias needs vendor-source equivalence evidence",
    )
    return target


def check_mapped_route(row: DispositionRow, effective_operation_id: str) -> None:
    """The EXACT operation (alias target when present) must be route-bound.

    Existence of the surface or method proves nothing on its own: an
    unrelated operation id cannot borrow a route just because the route's
    implementation exists.
    """
    if row.route.kind == "surface":
        require(
            row.route.target in SUPPORTED_SURFACES,
            "dispositions-surface-not-supported",
            f"{row.operation_id}: surface {row.route.target!r} is not in SUPPORTED_SURFACES",
        )
        require(
            effective_operation_id in SURFACE_OPERATION_BINDINGS.get(row.route.target, ()),
            "dispositions-surface-not-bound",
            f"{row.operation_id}: operation {effective_operation_id!r} is not reviewed"
            f" as reaching surface {row.route.target!r}",
        )
        return
    require(
        row.route.kind == "ops_read",
        "dispositions-route-invalid",
        f"{row.operation_id}: mapped rows route via surface or ops_read",
    )
    require(
        row.category == "read",
        "dispositions-ops-read-not-read",
        f"{row.operation_id}: ops_read routes are for read rows, not {row.category}",
    )
    require(
        row.route.target in OPS_READ_METHODS,
        "dispositions-ops-read-not-reviewed",
        f"{row.operation_id}: {row.route.target!r} is not a reviewed McpOps read method",
    )
    require(
        callable(getattr(McpOps, row.route.target, None)),
        "dispositions-ops-read-method-missing",
        f"{row.operation_id}: McpOps.{row.route.target} does not exist",
    )
    require(
        OPS_READ_OPERATION_BINDINGS.get(effective_operation_id) == row.route.target,
        "dispositions-ops-read-not-bound",
        f"{row.operation_id}: operation {effective_operation_id!r} is not reviewed"
        f" as served by McpOps.{row.route.target}",
    )


def check_risk(row: DispositionRow, op: OperationRow, target: DispositionRow | None) -> None:
    require(
        not op.requires_confirm_token or row.category == "guarded_mutation",
        "dispositions-risk-downgrade",
        f"{row.operation_id}: confirm-token operation must stay guarded_mutation"
        f" (got {row.category})",
    )
    session_id = (target.operation_id if target else None) or row.operation_id
    if op.safety in _DESTRUCTIVE_SAFETY:
        allowed = row.category in ("read", "guarded_mutation") or (
            row.category == "session_control" and session_id in SESSION_CONTROL_OPERATIONS
        )
        require(
            allowed,
            "dispositions-risk-downgrade",
            f"{row.operation_id}: {op.safety} operation downgraded to {row.category}",
        )
    elif op.safety == "unmapped":
        require(
            row.category != "mutate",
            "dispositions-risk-downgrade",
            f"{row.operation_id}: unknown vendor safety cannot be ordinary mutate",
        )
    require(
        row.category != "session_control" or session_id in SESSION_CONTROL_OPERATIONS,
        "dispositions-session-control-unreviewed",
        f"{row.operation_id}: session_control requires review-list membership",
    )
    if row.category == "read" and row.alias_of is None:
        name = op.action
        read_named = bool(READ_ACTION_RE.match(name)) or name in READ_ACTION_EXCEPTIONS
        read_named = read_named or name.endswith(READ_ACTION_SUFFIXES)
        require(
            read_named,
            "dispositions-read-unproven",
            f"{row.operation_id}: action {name!r} is not reviewably read-shaped",
        )


def check_evidence(
    row: DispositionRow, repo_root: Path, *, needs_repo_evidence: bool
) -> None:
    for ref in row.evidence:
        require(
            ref.strip() != "",
            "dispositions-evidence-missing",
            f"{row.operation_id}: empty ref",
        )
        if ref.startswith("vendor:"):
            require(
                bool(_VENDOR_REF_RE.match(ref)),
                "dispositions-evidence-format-invalid",
                f"{row.operation_id}: malformed vendor ref {ref!r}",
            )
        else:
            require(
                (repo_root / ref).is_file(),
                "dispositions-evidence-path-missing",
                f"{row.operation_id}: evidence path {ref!r} not found under {repo_root}",
            )
    if needs_repo_evidence:
        require(
            any(not ref.startswith("vendor:") for ref in row.evidence),
            "dispositions-evidence-missing",
            f"{row.operation_id}: mapped rows need repo evidence (test/probe path)",
        )


def require(ok: bool, code: str, detail: str) -> None:  # noqa: FBT001 (assert-style guard)
    if not ok:
        raise DispositionsError(code, detail)


__all__ = ["check_row", "require"]
