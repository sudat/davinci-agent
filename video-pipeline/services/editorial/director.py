"""Editorial Director adapter: the runtime model/tool boundary (Todo 39).

``EditorialDirector.run`` assembles a bounded evidence bundle through the
Todo-37 ``MediaQueryApi`` ONLY, builds the frozen versioned prompt contract,
records the Todo-12 data-policy envelope, and sends one request through the
replay-by-default transport. The result is ALWAYS a proposal-or-structured-
failure record: the director never writes plans or job state, never commits
Artifacts, never controls Resolve, never retries jobs, and holds no raw DB,
shell, file-write, or network handle. Commit authority belongs to Todo 41.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from services.contracts.editorial_model import EditorialRequestEnvelope
from services.editorial.evidence import EvidenceBundle, EvidenceIncomplete, assemble_evidence
from services.editorial.models import (
    DirectorRequest,
    DirectorRunResult,
    EditorialErrorRecord,
    EditorialMetadata,
    EditorialPolicyEnvelope,
)
from services.editorial.parse import ParsedProposal, parse_response
from services.editorial.pin import EditorialDirectorPin, load_pin
from services.editorial.policy import decide_transport_policy
from services.editorial.prompt import (
    PROMPT_CONTRACT_VERSION,
    build_prompt,
    prompt_bundle_hash,
    request_hash,
)
from services.editorial.transport import (
    EditorialModelRefusal,
    EditorialStrictResponse,
    EditorialTransport,
    EditorialTransportFailure,
    LiveTransport,
)

if TYPE_CHECKING:
    from services.media_query.api import MediaQueryApi

type _Status = Literal["proposal", "refusal", "error"]
type PolicyDecider = Callable[[str], EditorialPolicyEnvelope]


class EditorialDirector:
    """Proposal-only adapter over one transport and one concrete model pin."""

    __slots__ = ("_pin", "_policy_decider", "_transport", "_transport_kind")

    def __init__(
        self,
        *,
        transport: EditorialTransport,
        pin: EditorialDirectorPin | None = None,
        policy_decider: PolicyDecider | None = None,
        transport_kind: Literal["replay", "live-stub", "live-http", "live-codex-exec"]
        | None = None,
    ) -> None:
        self._transport = transport
        self._pin = pin if pin is not None else load_pin()
        self._policy_decider = policy_decider if policy_decider is not None else (
            decide_transport_policy
        )
        self._transport_kind: Literal[
            "replay", "live-stub", "live-http", "live-codex-exec"
        ] = (
            transport_kind
            if transport_kind is not None
            else ("live-stub" if isinstance(transport, LiveTransport) else "replay")
        )

    def run(self, request: DirectorRequest, *, api: MediaQueryApi) -> DirectorRunResult:
        policy = self._policy_decider(request.episode_id)
        bundle_hash = prompt_bundle_hash(build_prompt(request))
        if not policy.allowed:
            return self._result(
                request, policy, bundle_hash, lineage=(),
                error=EditorialErrorRecord(
                    code="local_only_denial", detail=policy.binding_reason
                ),
            )
        try:
            evidence = assemble_evidence(api, request)
        except EvidenceIncomplete as incomplete:
            return self._result(
                request, policy, bundle_hash, lineage=(),
                error=EditorialErrorRecord(
                    code="evidence_incomplete", detail=incomplete.detail
                ),
            )
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, self._pin.pin_version)
        metadata = EditorialMetadata(
            pin_version=self._pin.pin_version,
            requested_model=self._pin.model_id,
            transport_kind=self._transport_kind,
            prompt_bundle_hash=bundle_hash,
            evidence_lineage=evidence.lineage,
        )
        envelope = self._envelope(request, key, response_hash=None, status="error")
        outcome = self._transport.send(key)
        if not isinstance(
            outcome, EditorialModelRefusal | EditorialStrictResponse | EditorialTransportFailure
        ):
            outcome = EditorialTransportFailure(
                code="unknown_outcome",
                detail=f"transport returned an unknown outcome shape: {type(outcome).__name__}",
            )
        if isinstance(outcome, EditorialTransportFailure):
            return DirectorRunResult(
                envelope=envelope,
                policy=policy,
                metadata=metadata,
                error=EditorialErrorRecord(
                    code="transport_failure",
                    detail=outcome.detail,
                    transport_code=outcome.code,
                ),
            )
        if isinstance(outcome, EditorialModelRefusal):
            return DirectorRunResult(
                envelope=self._envelope(request, key, None, "refusal"),
                policy=policy,
                metadata=metadata,
                refusal_reason=outcome.reason,
            )
        response_hash = hashlib.sha256(outcome.payload).hexdigest()
        envelope = self._envelope(request, key, response_hash, "error")
        metadata = metadata.model_copy(update={"observed_model": outcome.served_by})
        parsed = parse_response(outcome.payload)
        if isinstance(parsed, EditorialErrorRecord):
            return DirectorRunResult(
                envelope=envelope, policy=policy, metadata=metadata, error=parsed
            )
        return self._checked_proposal(request, policy, metadata, evidence, envelope, parsed)

    def _checked_proposal(  # noqa: PLR0913, PLR0917 (one structured result assembly)
        self,
        request: DirectorRequest,
        policy: EditorialPolicyEnvelope,
        metadata: EditorialMetadata,
        evidence: EvidenceBundle,
        envelope: EditorialRequestEnvelope,
        parsed: ParsedProposal,
    ) -> DirectorRunResult:
        admissible = frozenset(evidence.admissible_segment_ids)
        invented = [
            entry.segment_id
            for entry in parsed.proposal.selection
            if entry.segment_id not in admissible
        ]
        if invented:
            return DirectorRunResult(
                envelope=envelope,
                policy=policy,
                metadata=metadata,
                error=EditorialErrorRecord(
                    code="hallucinated_reference",
                    detail=(
                        f"proposal references segment ids absent from the evidence bundle: "
                        f"{invented}; the model may never invent evidence or IDs"
                    ),
                ),
            )
        if parsed.proposal.episode_id != request.episode_id:
            return DirectorRunResult(
                envelope=envelope,
                policy=policy,
                metadata=metadata,
                error=EditorialErrorRecord(
                    code="proposal_schema_violation",
                    detail=(
                        f"proposal episode {parsed.proposal.episode_id} does not match the "
                        f"requested episode {request.episode_id}"
                    ),
                ),
            )
        return DirectorRunResult(
            envelope=self._envelope(
                request, envelope.request_hash, envelope.response_hash, "proposal"
            ),
            policy=policy,
            metadata=metadata,
            proposal=parsed.proposal,
        )

    def _envelope(
        self,
        request: DirectorRequest,
        key: str,
        response_hash: str | None,
        status: _Status,
    ) -> EditorialRequestEnvelope:
        return EditorialRequestEnvelope(
            episode_id=request.episode_id,
            model_role_id=self._pin.model_role_id,
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
            request_hash=key,
            response_hash=response_hash,
            status=status,
            external_credentials=(
                "env-var-not-recorded" if self._transport_kind == "live-http" else "none"
            ),
        )

    def _result(
        self,
        request: DirectorRequest,
        policy: EditorialPolicyEnvelope,
        bundle_hash: str,
        *,
        lineage: tuple[str, ...],
        error: EditorialErrorRecord,
    ) -> DirectorRunResult:
        metadata = EditorialMetadata(
            pin_version=self._pin.pin_version,
            requested_model=self._pin.model_id,
            transport_kind=self._transport_kind,
            prompt_bundle_hash=bundle_hash,
            evidence_lineage=lineage,
        )
        return DirectorRunResult(
            envelope=self._envelope(
                request, request_hash(PROMPT_CONTRACT_VERSION, lineage, self._pin.pin_version),
                None, "error",
            ),
            policy=policy,
            metadata=metadata,
            error=error,
        )


__all__ = ["EditorialDirector"]
