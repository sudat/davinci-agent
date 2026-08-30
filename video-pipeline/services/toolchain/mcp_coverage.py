"""Pinned-MCP operation inventory: alias normalization, cross-check, bytes.

Every vendor operation reachable at the pinned commit lands in one row keyed
by a stable underlying operation ID, so compound aliases and granular aliases
of one operation share that row. Normalization rules (mechanical and
deterministic — never guessed):

- a compound ``(tool, action)`` IS the underlying operation ``tool.action``;
- a granular tool ``g`` registered in module ``m`` is an alias of compound
  ``(m, a)`` iff ``m`` is a compound tool name and ``g == a`` or
  ``g == f"{m}_{a}"``; otherwise it is recorded as a granular-only
  operation ``m.g``.

Kernel catalog rows and granular/compound alias differences are cross-checked
and made explicit — orphan kernel rows, granular-only tools, and vendor-side
duplicate action entries are recorded, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from services.toolchain.mcp_coverage_models import (
    EXPECTED_COMPOUND_TOOLS,
    EXPECTED_GRANULAR_TOOLS,
    EXPECTED_KERNEL_ACTIONS,
    INVENTORY_SCHEMA,
    CompoundToolRow,
    CrossCheckFacts,
    GranularToolRow,
    InventoryCounts,
    LiveTool,
    McpCoverageDriftError,
    McpCoverageError,
    McpInventoryV1,
    OperationRow,
    PinFacts,
)

if TYPE_CHECKING:
    from services.toolchain.mcp_vendor_surface import VendorSurface

__all__ = [
    "EXPECTED_COMPOUND_TOOLS",
    "EXPECTED_GRANULAR_TOOLS",
    "EXPECTED_KERNEL_ACTIONS",
    "INVENTORY_SCHEMA",
    "LiveTool",
    "McpCoverageDriftError",
    "McpCoverageError",
    "McpInventoryV1",
    "PinFacts",
    "build_inventory",
    "inventory_bytes",
    "inventory_sha256",
    "schema_sha256",
]


def schema_sha256(schema: dict[str, object]) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_inventory(
    pin: PinFacts, surface: VendorSurface, live_tools: tuple[LiveTool, ...]
) -> McpInventoryV1:
    """Assemble the inventory; every drift against the pin fails typed."""
    static_names = tuple(tool.name for tool in surface.compound_tools)
    live_names = tuple(sorted(tool.name for tool in live_tools))
    _require_drift(
        ok=len(live_names) == EXPECTED_COMPOUND_TOOLS,
        code="compound-tool-count-drift",
        detail=f"live tools/list reports {len(live_names)} compound tools,"
        f" expected {EXPECTED_COMPOUND_TOOLS}",
    )
    _require_drift(
        ok=static_names == live_names,
        code="compound-tool-set-drift",
        detail=(
            f"static compound tools differ from live tools/list:"
            f" {static_names!r} vs {live_names!r}"
        ),
    )
    granular_count = sum(len(names) for _, names in surface.granular_modules)
    _require_drift(
        ok=granular_count == EXPECTED_GRANULAR_TOOLS,
        code="granular-tool-count-drift",
        detail=f"vendor granular modules register {granular_count} tools,"
        f" expected {EXPECTED_GRANULAR_TOOLS}",
    )
    _require_drift(
        ok=surface.kernel_catalog.stated_actions == EXPECTED_KERNEL_ACTIONS,
        code="kernel-action-count-drift",
        detail=f"kernel catalog states {surface.kernel_catalog.stated_actions} guarded actions,"
        f" expected {EXPECTED_KERNEL_ACTIONS}",
    )
    schemas = {tool.name: schema_sha256(tool.input_schema) for tool in live_tools}
    _require_drift(
        ok=set(schemas) == set(static_names),
        code="live-schema-coverage-drift",
        detail="live tools/list does not cover exactly the static compound tool set",
    )
    return _assemble(pin, surface, schemas, granular_count)


def _assemble(
    pin: PinFacts, surface: VendorSurface, schemas: dict[str, str], granular_count: int
) -> McpInventoryV1:
    actions_of = {tool.name: tool.actions for tool in surface.compound_tools}
    safety_of = {tool.name: tool.safety for tool in surface.compound_tools}
    kernel_pairs = frozenset((row.tool, row.action) for row in surface.kernel_catalog.rows)
    token_gated = frozenset(surface.confirm_token_actions)
    compound_rows = tuple(
        CompoundToolRow(
            name=tool.name,
            input_schema_sha256=schemas[tool.name],
            safety=tool.safety,
            actions=tool.actions,
        )
        for tool in surface.compound_tools
    )
    granular_rows = tuple(
        GranularToolRow(module=module, name=name)
        for module, names in surface.granular_modules
        for name in names
    )
    granular_aliases: dict[tuple[str, str], list[str]] = {}
    granular_only: list[str] = []
    for module, names in surface.granular_modules:
        if module not in actions_of:
            granular_only.extend(names)
            continue
        for name in names:
            target = _alias_target(module, name, actions_of[module])
            if target is None:
                granular_only.append(name)
            else:
                granular_aliases.setdefault((module, target), []).append(name)
    operations: list[OperationRow] = []
    for tool, actions in actions_of.items():
        operations.extend(
            OperationRow(
                operation_id=f"{tool}.{action}",
                tool=tool,
                action=action,
                source="compound",
                compound_aliases=(f"{tool}.{action}",),
                granular_aliases=tuple(sorted(granular_aliases.get((tool, action), ()))),
                safety=safety_of[tool],
                requires_confirm_token=(tool, action) in token_gated,
                kernel=(tool, action) in kernel_pairs,
            )
            for action in actions
        )
    for module, names in surface.granular_modules:
        operations.extend(
            OperationRow(
                operation_id=f"{module}.{name}",
                tool=module,
                action=name,
                source="granular_only",
                compound_aliases=(),
                granular_aliases=(name,),
                safety=safety_of.get(module, "unmapped"),
                requires_confirm_token=False,
                kernel=False,
            )
            for name in names
            if _alias_target(module, name, actions_of.get(module, ())) is None
        )
    operations.sort(key=lambda row: row.operation_id)
    kernel_tool_orphans = tuple(
        sorted({row.tool for row in surface.kernel_catalog.rows} - set(actions_of))
    )
    kernel_action_orphans = tuple(
        sorted(
            f"{row.tool}.{row.action}"
            for row in surface.kernel_catalog.rows
            if row.tool in actions_of and row.action not in actions_of[row.tool]
        )
    )
    return McpInventoryV1(
        schema_version=INVENTORY_SCHEMA,
        pin=pin,
        counts=InventoryCounts(
            compound_tools=len(surface.compound_tools),
            granular_tools=granular_count,
            kernel_actions=surface.kernel_catalog.stated_actions,
        ),
        compound_tools=compound_rows,
        granular_tools=tuple(sorted(granular_rows, key=lambda row: (row.module, row.name))),
        operations=tuple(operations),
        kernel_catalog_stated_actions=surface.kernel_catalog.stated_actions,
        kernel_catalog_stated_tools=surface.kernel_catalog.stated_tools,
        kernel_catalog_rows=len(surface.kernel_catalog.rows),
        cross_check=CrossCheckFacts(
            static_tool_names_match_live=True,
            compound_action_count=sum(len(actions) for actions in actions_of.values()),
            operation_count=len(operations),
            granular_alias_count=sum(len(v) for v in granular_aliases.values()),
            granular_only=tuple(sorted(granular_only)),
            kernel_action_orphans=kernel_action_orphans,
            kernel_tool_orphans=kernel_tool_orphans,
            intra_tool_duplicate_actions=tuple(
                sorted(f"{tool}.{action}" for tool, action in surface.duplicate_actions)
            ),
        ),
    )


def _alias_target(
    module: str, granular_name: str, compound_actions: tuple[str, ...]
) -> str | None:
    """The compound action a granular tool mechanically aliases, if any."""
    for action in compound_actions:
        if granular_name in (action, f"{module}_{action}"):
            return action
    return None


def _require_drift(*, ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise McpCoverageDriftError(code, detail)


def inventory_bytes(inventory: McpInventoryV1) -> bytes:
    """Deterministic compact UTF-8 JSON (sorted keys), manifest-v1 convention."""
    payload = json.dumps(
        inventory.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return payload.encode("utf-8")


def inventory_sha256(inventory: McpInventoryV1) -> str:
    return hashlib.sha256(inventory_bytes(inventory)).hexdigest()
