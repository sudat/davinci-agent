"""Editorial Director service: the runtime model/tool boundary (PRD 11.1).

Proposals ONLY — commit authority belongs to the validators/committers
(Todo 41). Evidence arrives exclusively through the bounded Todo-37
MediaQueryApi; the deterministic replay transport is the default; the live
stub is credential-gated and network-free.
"""

from services.editorial.director import EditorialDirector
from services.editorial.evidence import (
    Corroboration,
    EvidenceBundle,
    EvidenceIncomplete,
    assemble_evidence,
)
from services.editorial.models import (
    DeclaredCandidate,
    DirectorRequest,
    DirectorRunResult,
    EditorialErrorRecord,
    EditorialMetadata,
    EditorialPolicyEnvelope,
)
from services.editorial.parse import parse_response
from services.editorial.pin import (
    PIN_PATH,
    EditorialDirectorPin,
    EditorialReplaySet,
    PinError,
    load_pin,
    load_replay_set,
)
from services.editorial.policy import decide_transport_policy
from services.editorial.prompt import (
    PROMPT_CONTRACT_VERSION,
    SYSTEM_PROMPT,
    PromptBundle,
    build_prompt,
    prompt_bundle_hash,
    request_hash,
)
from services.editorial.transport import (
    CREDENTIALS_ENV,
    POLICY_ENV,
    EditorialModelRefusal,
    EditorialOutcome,
    EditorialStrictResponse,
    EditorialTransport,
    EditorialTransportFailure,
    LiveTransport,
    ReplayTransport,
    replay_response,
)

__all__ = [
    "CREDENTIALS_ENV",
    "PIN_PATH",
    "POLICY_ENV",
    "PROMPT_CONTRACT_VERSION",
    "SYSTEM_PROMPT",
    "Corroboration",
    "DeclaredCandidate",
    "DirectorRequest",
    "DirectorRunResult",
    "EditorialDirector",
    "EditorialDirectorPin",
    "EditorialErrorRecord",
    "EditorialMetadata",
    "EditorialModelRefusal",
    "EditorialOutcome",
    "EditorialPolicyEnvelope",
    "EditorialReplaySet",
    "EditorialStrictResponse",
    "EditorialTransport",
    "EditorialTransportFailure",
    "EvidenceBundle",
    "EvidenceIncomplete",
    "LiveTransport",
    "PinError",
    "PromptBundle",
    "ReplayTransport",
    "assemble_evidence",
    "build_prompt",
    "decide_transport_policy",
    "load_pin",
    "load_replay_set",
    "parse_response",
    "prompt_bundle_hash",
    "replay_response",
    "request_hash",
]
