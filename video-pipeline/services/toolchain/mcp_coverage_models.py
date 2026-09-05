"""Strict models and drift-guard constants for the pinned MCP inventory.

The expected counts (36 compound tools / 353 granular tools / 136 guarded
kernel actions) are DRIFT GUARDS tied to the current pin — a mismatch is a
typed :class:`McpCoverageDriftError` and is never resolved by editing these
constants. All tuples are canonically sorted by the builder so serialized
bytes are deterministic across generations.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel

INVENTORY_SCHEMA: Final = "mcp-inventory-v1"
EXPECTED_COMPOUND_TOOLS: Final = 36
EXPECTED_GRANULAR_TOOLS: Final = 353
EXPECTED_KERNEL_ACTIONS: Final = 136


class McpCoverageError(Exception):
    """Inventory construction or serialization failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class McpCoverageDriftError(McpCoverageError):
    """The measured vendor surface differs from the pinned drift guards."""


class PinFacts(StrictModel):
    commit: str
    provider_version: str
    server_mode: str
    advanced_enabled: bool
    handshake_name: str
    handshake_version: str


class CompoundToolRow(StrictModel):
    name: str
    input_schema_sha256: Sha256
    safety: str
    actions: tuple[str, ...]


class GranularToolRow(StrictModel):
    module: str
    name: str


class OperationRow(StrictModel):
    operation_id: str
    tool: str
    action: str
    source: Literal["compound", "granular_only"]
    compound_aliases: tuple[str, ...]
    granular_aliases: tuple[str, ...]
    safety: str
    requires_confirm_token: bool
    kernel: bool


class CrossCheckFacts(StrictModel):
    static_tool_names_match_live: bool
    compound_action_count: int
    operation_count: int
    granular_alias_count: int
    granular_only: tuple[str, ...]
    kernel_action_orphans: tuple[str, ...]
    kernel_tool_orphans: tuple[str, ...]
    intra_tool_duplicate_actions: tuple[str, ...]


class InventoryCounts(StrictModel):
    compound_tools: int
    granular_tools: int
    kernel_actions: int


class McpInventoryV1(StrictModel):
    schema_version: str
    pin: PinFacts
    counts: InventoryCounts
    compound_tools: tuple[CompoundToolRow, ...]
    granular_tools: tuple[GranularToolRow, ...]
    operations: tuple[OperationRow, ...]
    kernel_catalog_stated_actions: int
    kernel_catalog_stated_tools: int
    kernel_catalog_rows: int
    cross_check: CrossCheckFacts


class LiveTool(StrictModel):
    """One ``tools/list`` entry from the pinned server (name + schema)."""

    name: str = Field(min_length=1)
    input_schema: dict[str, object]


__all__ = [
    "EXPECTED_COMPOUND_TOOLS",
    "EXPECTED_GRANULAR_TOOLS",
    "EXPECTED_KERNEL_ACTIONS",
    "INVENTORY_SCHEMA",
    "CompoundToolRow",
    "CrossCheckFacts",
    "GranularToolRow",
    "InventoryCounts",
    "LiveTool",
    "McpCoverageDriftError",
    "McpCoverageError",
    "McpInventoryV1",
    "OperationRow",
    "PinFacts",
]
