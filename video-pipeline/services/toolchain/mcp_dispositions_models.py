"""Strict models and reviewed allowlists for the MCP parity dispositions.

The dispositions artifact (``mcp-dispositions-v1``) gives every underlying
operation of the pinned inventory exactly one reviewed row: a risk category,
a concrete product route, a rollout status, and evidence references. Two
allowlists below are REVIEW LOCKS — they encode what the Task 10 review
verified about production symbols and the vendor surface:

- ``OPS_READ_METHODS`` — typed ``McpOps`` methods that are genuinely
  read-only APIs (a mutation method can never back an ``ops_read`` route);
- ``SESSION_CONTROL_OPERATIONS`` — the only operations whose risk category
  may be ``session_control`` (app/session lifecycle state, not project
  content; reviewed against the vendor source).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel

DISPOSITIONS_SCHEMA: Final = "mcp-dispositions-v1"

DispositionCategory = Literal["read", "mutate", "guarded_mutation", "session_control"]
DispositionStatus = Literal["mapped", "deferred"]
RouteKind = Literal["surface", "ops_read", "planned"]
#: Deferred rows carry the planned route class, one per Phase 4 mapping task
#: (Task 13 owns read and session operations).
PLANNED_ROUTE_TARGETS: Final[frozenset[str]] = frozenset(
    ("task13:read-session", "task14:plan-step-surface", "task15:guarded-handler")
)

#: Typed McpOps methods the review verified as read-only APIs (Task 10).
#: ``subtitle_generation_probe`` is deliberately absent: the vendor action can
#: generate captions (``allow_generate``) and is therefore guarded, not read.
OPS_READ_METHODS: Final[frozenset[str]] = frozenset(
    (
        "get_current_project",
        "detect_gaps_overlaps",
        "detect_missing_media",
        "get_transform",
        "voice_isolation_capabilities",
        "edit_kernel_capabilities",
        "audio_mix_capability_report",
        "fusion_comp_count",
        "render_get_settings",
        "find_similar",
        "edit_engine_plan_selects",
    )
)

#: Operations reviewed as app/session lifecycle control (vendor source read);
#: nothing outside this lock may be classified ``session_control``.
SESSION_CONTROL_OPERATIONS: Final[frozenset[str]] = frozenset(
    (
        "resolve_control.launch",
        "resolve_control.quit",
        "resolve_control.restore_state",
        "resolve_control.save_state",
        "resolve_control.open_page",
        "resolve_control.close_control_panel",
        "resolve_control.open_control_panel",
        "resolve_control.set_high_priority",
        "resolve_control.disable_background_tasks_for_current_session",
        "resolve_control.snooze_mcp_update",
        "resolve_control.ignore_mcp_update",
        "resolve_control.set_mcp_update_policy",
        "resolve_control.clear_mcp_update_preferences",
        "resolve_control.restart_app",
        "resolve_control.open_app_preferences",
        "resolve_control.open_settings",
        "timeline_versioning.begin_run",
        "timeline_versioning.end_run",
        "setup.set_defaults",
        "setup.clear_defaults",
    )
)

#: An action may be classified ``read`` only when its name is read-shaped or
#: explicitly reviewed here (blocks relabeling mutations as reads).
READ_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(get_|list|is_|probe_|plan_|propose_|detect_gaps|detect_missing|"
    r"detect_entities|detect_shot|detect_sync|search_|find_|diff_|compare_)"
)
READ_ACTION_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    (
        "action_help", "object_help", "api_truth", "check_version_support",
        "schema", "path", "read", "validate", "journal", "runtime_mode",
        "take_diff", "summarize", "conform_lint", "rule_of_six_audit",
        "sound_density_audit", "split_edit_audit", "first_impression",
        "setup_sheet", "rank_takes", "cut_candidates",
        # knowledge tool (v2.207.0, src/server.py:31327): read-only guide
        # queries — bare verb forms the prefix regex cannot see.
        "topics", "get", "search",
        # resolve_control.inspect_operation (v2.210.0, execution_lifecycle
        # inspect_operation): classifies pre-flight risk and reads state —
        # never executes the inspected action.
        "inspect_operation",
    )
)
#: …or end in one of these reviewed read-only suffixes.
READ_ACTION_SUFFIXES: Final[tuple[str, ...]] = (
    "_capabilities", "_report", "_status", "capabilities",
)


class DispositionsError(Exception):
    """Dispositions loading or validation failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class DispositionsParityError(DispositionsError):
    """Strict (final parity) mode found remaining gaps."""


class RouteRef(StrictModel):
    kind: RouteKind
    target: str = Field(min_length=1)


class DispositionRow(StrictModel):
    operation_id: str = Field(min_length=3)
    category: DispositionCategory
    status: DispositionStatus
    route: RouteRef
    #: granular-only rows may alias a reviewed compound operation and then
    #: inherit its category/status/route (validator-enforced, no chains).
    alias_of: str | None = None
    owner: str = ""
    reason: str = ""
    target_phase: str = ""
    evidence: tuple[str, ...] = Field(min_length=1)


class DispositionsV1(StrictModel):
    schema_version: str
    pin_commit: str
    inventory_sha256: Sha256
    rows: tuple[DispositionRow, ...]


class CoverageReportV1(StrictModel):
    """Rollout/final gap report over the full disposition set."""

    mode: Literal["rollout", "final"]
    total_operations: int
    mapped: int
    deferred: int
    unmapped: int
    refused_vendor_supported: int
    coverage: float
    deferred_by_category: dict[str, int]
    deferred_by_domain: dict[str, int]


__all__ = [
    "DISPOSITIONS_SCHEMA",
    "OPS_READ_METHODS",
    "PLANNED_ROUTE_TARGETS",
    "READ_ACTION_EXCEPTIONS",
    "READ_ACTION_RE",
    "READ_ACTION_SUFFIXES",
    "SESSION_CONTROL_OPERATIONS",
    "CoverageReportV1",
    "DispositionCategory",
    "DispositionRow",
    "DispositionStatus",
    "DispositionsError",
    "DispositionsParityError",
    "DispositionsV1",
    "RouteKind",
    "RouteRef",
]
