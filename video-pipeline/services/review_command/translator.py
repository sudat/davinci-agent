"""Policy-gated natural-language Review Translator adapter (phase 0C Spike).

SUNSET: before the post-0C Control Plane exists, the adapter-local deny-by-default
allowlist in ``translator_policy`` may authorize ONLY the frozen synthetic
phase-0C Cloud fixtures. It is temporary, never authorizes Production Episode
data, and is superseded by Control Plane policy after Todo 12 (see
``SPIKE_ALLOWLIST_SUNSET_NOTE``).

Contract shape: the model output is ALWAYS a proposal (``actor_intent=model``)
and NEVER an approval — ``approve_remaining`` / ``approve_editorial_plan`` are
human-only per Todo 28 and this adapter refuses to construct them. Every
failure (denial, refusal, truncation, malformed JSON, unknown or human-only
kind, transport error) yields a structured record without a proposal, so no
failure path can ever reach a committed proposal; the success path still goes
through Todo-28 ``validate_proposal`` downstream.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from services.review_command.models import (
    HumanApprovalProposal0C,
    parse_proposal,
)
from services.review_command.translator_policy import (
    DEFAULT_TOOLCHAIN_LOCK,
    PolicySourceError,
    authorize,
    load_frozen_contract,
)
from services.review_command.translator_record import (
    TRANSLATOR_RECORD_SCHEMA_VERSION,
    TranslationResult,
    TranslatorErrorCode,
    TranslatorErrorRecord,
    TranslatorRecord,
    TranslatorRefusal,
)
from services.review_command.translator_schema import (
    ALL_COMMAND_KINDS,
    HUMAN_ONLY_COMMAND_KINDS,
    PROMPT_CONTRACT_VERSION,
    TranslatorRequest,
    request_hash,
)
from services.review_command.translator_transport import (
    ModelRefusal,
    StrictResponse,
    Transport,
    TransportFailure,
)


def _record(
    request: TranslatorRequest,
    *,
    response_hash: str | None,
    proposal_id: str | None = None,
    refusal_reason: str | None = None,
    error_code: str | None = None,
) -> TranslatorRecord:
    status = (
        "proposal"
        if proposal_id is not None
        else "refusal"
        if refusal_reason is not None
        else "error"
    )
    return TranslatorRecord(
        schema_version=TRANSLATOR_RECORD_SCHEMA_VERSION,
        episode_id=request.episode_id,
        policy_profile_id=request.policy_profile_id,
        prompt_contract_version=PROMPT_CONTRACT_VERSION,
        request_hash=request_hash(request),
        response_hash=response_hash,
        status=status,
        instruction=request.instruction,
        proposal_id=proposal_id,
        refusal_reason=refusal_reason,
        error_code=error_code,
    )


def _error(
    request: TranslatorRequest,
    code: TranslatorErrorCode,
    detail: str,
    *,
    transport_code: str | None = None,
    response_hash: str | None = None,
) -> TranslationResult:
    error = TranslatorErrorRecord(code=code, detail=detail, transport_code=transport_code)
    record = _record(request, response_hash=response_hash, error_code=code)
    return TranslationResult(
        status="error", proposal=None, refusal=None, error=error, record=record
    )


def _classify_decode_failure(text: str, error: json.JSONDecodeError) -> TranslatorErrorCode:
    stripped_length = len(text.rstrip())
    if "Unterminated" in error.msg or (
        error.msg.startswith("Expecting") and error.pos >= stripped_length - 1
    ):
        return "truncated_response"
    return "malformed_json"


def _guard_document(document: dict[object, object]) -> tuple[TranslatorErrorCode, str] | None:
    kind: object = document.get("command_kind")
    if not isinstance(kind, str):
        return ("unknown_command_kind", "command_kind is missing or not a string")
    if kind not in ALL_COMMAND_KINDS:
        return ("unknown_command_kind", f"unknown command kind {quote(kind)}")
    if kind in HUMAN_ONLY_COMMAND_KINDS:
        detail = (
            f"{kind} is human-only per the proposal contract; the translator refuses "
            f"to construct approvals"
        )
        return ("human_only_command_kind", detail)
    intent: object = document.get("actor_intent")
    if intent != "model":
        return (
            "model_intent_violation",
            f"translator output must carry actor_intent=model, got {intent!r}",
        )
    return None


def _parse_strict_response(
    request: TranslatorRequest, payload: bytes, response_hash: str
) -> TranslationResult:
    text = payload.decode("utf-8", errors="replace")
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError as error:
        code = _classify_decode_failure(text, error)
        return _error(request, code, error.msg, response_hash=response_hash)
    if not isinstance(document, dict):
        return _error(
            request, "malformed_json", "response is not a JSON object", response_hash=response_hash
        )
    guard = _guard_document(document)
    if guard is not None:
        return _error(request, guard[0], guard[1], response_hash=response_hash)
    try:
        proposal = parse_proposal(payload)
    except (ValidationError, PydanticCustomError) as error:
        return _error(request, "proposal_schema_violation", str(error), response_hash=response_hash)
    if isinstance(proposal, HumanApprovalProposal0C) or proposal.actor_intent != "model":
        return _error(
            request,
            "model_intent_violation",
            "post-parse invariant failed: translator proposals are model-intent edit commands",
            response_hash=response_hash,
        )
    record = _record(request, response_hash=response_hash, proposal_id=proposal.proposal_id)
    return TranslationResult(
        status="proposal", proposal=proposal, refusal=None, error=None, record=record
    )


def translate(
    request: TranslatorRequest,
    *,
    transport: Transport,
    lock_path: Path = DEFAULT_TOOLCHAIN_LOCK,
) -> TranslationResult:
    """Run the policy-gated translation; denial happens before any transport."""

    try:
        contract = load_frozen_contract(lock_path)
    except PolicySourceError as error:
        return _error(request, "local_only_denial", str(error))
    denial = authorize(
        episode_id=request.episode_id,
        policy_profile_id=request.policy_profile_id,
        schema_version=request.schema_version,
        contract=contract,
    )
    if denial is not None:
        return _error(request, "local_only_denial", denial)

    outcome = transport.send(request_hash(request))
    if isinstance(outcome, ModelRefusal):
        record = _record(request, response_hash=None, refusal_reason=outcome.reason)
        return TranslationResult(
            status="refusal",
            proposal=None,
            refusal=TranslatorRefusal(reason=outcome.reason),
            error=None,
            record=record,
        )
    if isinstance(outcome, TransportFailure):
        return _error(
            request,
            "transport_failure",
            outcome.detail,
            transport_code=outcome.code,
        )
    if isinstance(outcome, StrictResponse):
        response_hash = hashlib.sha256(outcome.payload).hexdigest()
        return _parse_strict_response(request, outcome.payload, response_hash)
    return _error(
        request,
        "transport_failure",
        f"transport returned an unknown outcome shape: {type(outcome).__name__}",
        transport_code="unknown_outcome",
    )
