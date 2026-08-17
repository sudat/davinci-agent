from typing import Final

PHASE_1_CONTROL_PLANE_CRITERIA: Final = (
    "phase-1-cp-atomic-publication",
    "phase-1-cp-crash-reconciliation",
    "phase-1-cp-lease-authority",
    "phase-1-cp-stale-cas-suppression",
    "phase-1-cp-idempotent-resume",
)

CONTROL_PLANE_FIXTURES: Final = (
    "cp-atomic-publish",
    "cp-crash-before-rename",
    "cp-orphan-reconcile",
    "cp-stale-cas",
    "cp-lease-expiry",
    "cp-path-symlink-denial",
)
