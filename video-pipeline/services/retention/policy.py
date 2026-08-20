"""Policy loading plus state-aware classification, holds, and eligibility.

Conservative by construction: any name the policy does not classify as
sweepable is retained, and only a verified FROZEN state whose retention
period has elapsed can ever unlock sweepable classes (PRD 28.2 —
retention is tied to the job's FROZEN/FAILED/ACTIVE states).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from services.retention.errors import RetentionError
from services.retention.models import (
    JOB_STATE_FILE,
    REGISTRY_FILE,
    RetentionPolicy,
)

if TYPE_CHECKING:
    from services.retention.models import JobRetentionState
    from services.retention.records import AuditReason

PROTECTED_NAMES: frozenset[str] = frozenset({JOB_STATE_FILE, REGISTRY_FILE})

NameClass = Literal[
    "authoritative",
    "manual-finalization",
    "rebuildable",
    "runtime-cache",
    "protected",
    "unclassified",
]

_HOLD_REASONS: dict[str, AuditReason] = {
    "ACTIVE": "hold-active",
    "NEEDS_HUMAN": "hold-needs-human",
    "FAILED": "hold-failed",
}


def load_policy(path: Path) -> RetentionPolicy:
    """Load and validate a policy JSON file; malformed input is a typed error."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RetentionError("policy-invalid", f"cannot read policy {path}: {error}") from error
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RetentionError("policy-invalid", f"policy is not valid JSON: {error}") from error
    try:
        return RetentionPolicy.model_validate(payload)
    except ValidationError as error:
        raise RetentionError("policy-invalid", f"policy rejected by schema: {error}") from error


def classify_name(name: str, policy: RetentionPolicy) -> NameClass:
    """Map one top-level job-dir entry name to its retention class."""

    if name in PROTECTED_NAMES:
        return "protected"
    if name in policy.authoritative_names:
        return "authoritative"
    if name in policy.manual_finalization_names:
        return "manual-finalization"
    if name in policy.rebuildable_names:
        return "rebuildable"
    if name in policy.runtime_cache_names:
        return "runtime-cache"
    return "unclassified"


def hold_reason(state: JobRetentionState | None) -> AuditReason | None:
    """Investigation-hold reason for a sweepable class; None when eligible."""

    if state is None:
        return "hold-missing-state"
    if state.status == "FROZEN":
        return None
    return _HOLD_REASONS[state.status]


def sweep_unlocked(
    state: JobRetentionState | None, policy: RetentionPolicy, now_epoch_s: int
) -> bool:
    """True only for a verified FROZEN state whose retention period elapsed."""

    if state is None or state.status != "FROZEN" or state.frozen_at_epoch_s is None:
        return False
    elapsed = now_epoch_s - state.frozen_at_epoch_s
    return elapsed >= policy.rebuildable_retention_days * 86_400


__all__ = [
    "PROTECTED_NAMES",
    "NameClass",
    "classify_name",
    "hold_reason",
    "load_policy",
    "sweep_unlocked",
]
