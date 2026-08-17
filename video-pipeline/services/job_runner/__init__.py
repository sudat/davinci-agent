"""Job runner Control Plane: SQLite runtime state, migrations, integrity.

Public surface for Todos 9-11: ``StateStore`` (jobs, stage runs,
leases, approval refs, cache pointers), numbered SQL migrations, and
the fail-closed ``verify_against_store`` / ``recover_job`` queries.
The database is runtime/control state only — canonical Artifacts and
committed Plan bodies stay in the file artifact store.
"""

from services.job_runner.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_integrity import recover_job, verify_against_store
from services.job_runner.state_models import (
    ApprovalRefRow,
    CachePointerRow,
    JobRecovery,
    JobRow,
    JobSnapshot,
    JobStatus,
    LeaseRow,
    PragmaState,
    StageRunRow,
    StageRunStatus,
    VerificationReport,
)
from services.job_runner.state_store import StateStore

__all__ = [
    "LATEST_VERSION",
    "MIGRATIONS",
    "ApprovalRefRow",
    "CachePointerRow",
    "JobRecovery",
    "JobRow",
    "JobSnapshot",
    "JobStatus",
    "LeaseRow",
    "Migration",
    "MigrationError",
    "PragmaState",
    "StageRunRow",
    "StageRunStatus",
    "StateStore",
    "StateStoreError",
    "VerificationReport",
    "apply_migrations",
    "recover_job",
    "verify_against_store",
]
