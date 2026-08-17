"""Purpose-bound local operator operation records (Todo 13).

An OperationRecord is an audit checkpoint for an approved single-user
host: one approval purpose bound to one target bundle hash, captured at
a real controlling terminal. This is NOT cryptographic identity and NOT
non-repudiation: ``uid``/``tty`` are local-host observations, and the
store's hash chain detects only after-the-fact mutation of the record
log by this process. Product AI / non-interactive runners can never
produce operator records; the TTY ingress refuses them.
"""

from services.approvals.ingress import (
    IngressRefusalError,
    record_fixture_operation,
    record_operation,
)
from services.approvals.models import (
    PURPOSE_TARGET_TYPES,
    ApprovalPurpose,
    ApprovalTargetType,
    ChainedOperationRecord,
    OperationDraft,
    OperationRecord,
)
from services.approvals.store import (
    GENESIS_RECORD_HASH,
    OperationRecordError,
    OperationRecordStore,
)
from services.approvals.verify import (
    AuthorizationVerdict,
    VerificationError,
    evaluate_authorization,
    validate_operation_record,
    validate_supersession_chain,
)

__all__ = [
    "GENESIS_RECORD_HASH",
    "PURPOSE_TARGET_TYPES",
    "ApprovalPurpose",
    "ApprovalTargetType",
    "AuthorizationVerdict",
    "ChainedOperationRecord",
    "IngressRefusalError",
    "OperationDraft",
    "OperationRecord",
    "OperationRecordError",
    "OperationRecordStore",
    "VerificationError",
    "evaluate_authorization",
    "record_fixture_operation",
    "record_operation",
    "validate_operation_record",
    "validate_supersession_chain",
]
