"""Result and evidence-record models for the phase-0C Review Translator.

``TranslatorRecord`` is the artifact-style evidence of one translation call:
request hash, response hash, prompt-contract version, and status. It contains
no secrets and no raw media text beyond the declared untrusted ``instruction``
field; plan context never enters the record.
"""

from __future__ import annotations

from typing import Literal

from services.contracts.primitives import StrictModel
from services.foundation_io import canonical_model_bytes
from services.review_command.models import ReviewCommandProposal0C  # noqa: TC001 (pydantic runtime)

TRANSLATOR_RECORD_SCHEMA_VERSION = "translator-record-0c-v1"

type TranslationStatus = Literal["proposal", "refusal", "error"]
type TranslatorErrorCode = Literal[
    "local_only_denial",
    "truncated_response",
    "malformed_json",
    "proposal_schema_violation",
    "unknown_command_kind",
    "human_only_command_kind",
    "model_intent_violation",
    "transport_failure",
]


class TranslatorErrorRecord(StrictModel):
    code: TranslatorErrorCode
    detail: str
    transport_code: str | None = None


class TranslatorRefusal(StrictModel):
    reason: str


class TranslatorRecord(StrictModel):
    schema_version: Literal["translator-record-0c-v1"]
    episode_id: str
    policy_profile_id: str
    prompt_contract_version: str
    request_hash: str
    response_hash: str | None
    status: TranslationStatus
    instruction: str
    proposal_id: str | None = None
    refusal_reason: str | None = None
    error_code: str | None = None

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)


class TranslationResult(StrictModel):
    status: TranslationStatus
    proposal: ReviewCommandProposal0C | None
    refusal: TranslatorRefusal | None
    error: TranslatorErrorRecord | None
    record: TranslatorRecord
