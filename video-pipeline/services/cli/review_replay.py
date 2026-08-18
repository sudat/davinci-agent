"""The Phase-1 review translator: declared replay + Todo-12 policy gate.

``propose_review_command`` wraps the Todo-29 replay pattern (deterministic,
in-process, keyed by the exact request hash) with the Todo-12 Control-Plane
policy gate (the resolved production snapshot): the snapshot must DECLARE the
review stage's data class, the cloud-transport decision is recorded, and the
only executable transport is the local replay. Instructions are translated
ONLY when they exactly match one canonical rendering of the episode's frozen
declared correction sequence — an unknown or malformed instruction is a typed
``replay_mismatch`` error, never a silently coerced proposal. Classification
is always recomputed from the committed plan via the Todo-28 validator.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from services.cli.review_instructions import (
    canonical_instruction,
    declared_proposal_payload,
)
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.policy.data_policy import authorize_cloud_transport
from services.review_command.models import ReviewCommandProposal0C
from services.review_command.translator_schema import (
    TranslatorRequest,
    plan_context_from_edit_plan,
    request_hash,
)
from services.review_command.validate import (
    ProposalValidationError,
    validate_proposal,
)

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.fixtures.manifest_phase1 import (
        Phase1TechnicalFixtureManifest,
    )

REVIEW_DATA_CLASS = "review_instruction_text"
REVIEW_STAGE = "review_translate"
REPLAY_POLICY_PROFILE_ID = "phase-0c-deterministic-classifier-v1"
REPLAY_SCHEMA_VERSION = "preview-review-v1"

type Classification = Literal["clear", "ambiguous", "conflict"]


class ReviewTranslateError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PolicyGate(StrictModel):
    schema_version: Literal["review-policy-gate-v1"]
    stage_declared: bool
    cloud_decision: str
    cloud_reason: str
    production_policy_sha256: Sha256
    translator_policy_sha256: Sha256


class ProposalOutcome(StrictModel):
    schema_version: Literal["review-proposal-v1"]
    episode_id: Identifier
    instruction: str
    status: Literal["proposal", "error"]
    error_code: str | None = None
    error_detail: str | None = None
    request_hash: Sha256 | None = None
    policy: PolicyGate
    proposal_json: str | None = None
    proposal_sha256: Sha256 | None = None
    base_plan_version: str | None = None
    base_plan_hash: Sha256 | None = None
    classification: Classification | None = None
    candidate_item_ids: tuple[str, ...] = ()
    ambiguity_reasons: tuple[str, ...] = ()
    command_index: int | None = None


@dataclass(frozen=True, slots=True)
class PlanView:
    """The current committed plan the translator validates against."""

    plan: EditPlan0C
    version: str
    plan_hash: str


_PROPOSAL_ADAPTER = TypeAdapter(ReviewCommandProposal0C)
_IDENTIFIER = TypeAdapter(Identifier)


def review_policy_gate(
    policy: ResolvedConfig,
    *,
    episode_id: str,
    policy_sha: str,
    translator_sha: str,
) -> PolicyGate:
    declared = any(
        entry.stage == REVIEW_STAGE and REVIEW_DATA_CLASS in entry.classes
        for entry in policy.data_classes
    )
    if not declared:
        raise ReviewTranslateError(
            "stage_not_declared",
            f"the resolved policy does not declare data class {REVIEW_DATA_CLASS} "
            f"at stage {REVIEW_STAGE}; refusing to translate",
        )
    decision = authorize_cloud_transport(
        policy, data_class=REVIEW_DATA_CLASS, stage=REVIEW_STAGE, episode_id=episode_id
    )
    return PolicyGate(
        schema_version="review-policy-gate-v1",
        stage_declared=True,
        cloud_decision=decision.decision,
        cloud_reason=decision.reason,
        production_policy_sha256=policy_sha,
        translator_policy_sha256=translator_sha,
    )


def propose_review_command(  # noqa: PLR0913 (translator adapter contract)
    manifest: Phase1TechnicalFixtureManifest,
    instruction: str,
    current: PlanView,
    policy: ResolvedConfig,
    *,
    policy_sha: str,
    translator_sha: str,
) -> ProposalOutcome:
    gate = review_policy_gate(
        policy, episode_id=manifest.fixture_id, policy_sha=policy_sha, translator_sha=translator_sha
    )
    text = instruction.strip()
    request = TranslatorRequest(
        episode_id=_IDENTIFIER.validate_python(manifest.fixture_id),
        policy_profile_id=_IDENTIFIER.validate_python(REPLAY_POLICY_PROFILE_ID),
        schema_version=_IDENTIFIER.validate_python(REPLAY_SCHEMA_VERSION),
        instruction=text,
        plan_context=plan_context_from_edit_plan(current.plan),
    )
    digest = request_hash(request)
    matched = next(
        (
            command
            for command in manifest.review_commands
            if canonical_instruction(command) == text
        ),
        None,
    )
    if matched is None:
        return ProposalOutcome(
            schema_version="review-proposal-v1",
            episode_id=manifest.fixture_id,
            instruction=text,
            status="error",
            error_code="replay_mismatch",
            error_detail=(
                "the instruction matches no canonical rendering of the episode's declared "
                "correction sequence; refusing to coerce it into a proposal"
            ),
            request_hash=digest,
            policy=gate,
        )
    proposal = _PROPOSAL_ADAPTER.validate_python(
        declared_proposal_payload(
            manifest, matched, instruction=text, base_version=current.version
        )
    )
    proposal_json = canonical_model_bytes(proposal).decode()
    try:
        outcome = validate_proposal(current.plan, proposal)
    except ProposalValidationError as error:
        return ProposalOutcome(
            schema_version="review-proposal-v1",
            episode_id=manifest.fixture_id,
            instruction=text,
            status="error",
            error_code=error.code,
            error_detail=error.detail,
            request_hash=digest,
            policy=gate,
        )
    return ProposalOutcome(
        schema_version="review-proposal-v1",
        episode_id=manifest.fixture_id,
        instruction=text,
        status="proposal",
        request_hash=digest,
        policy=gate,
        proposal_json=proposal_json,
        proposal_sha256=hashlib.sha256(proposal_json.encode()).hexdigest(),
        base_plan_version=current.version,
        base_plan_hash=current.plan_hash,
        classification=outcome.classification,
        candidate_item_ids=outcome.candidate_item_ids,
        ambiguity_reasons=outcome.ambiguity_reasons,
        command_index=matched.command_index,
    )


__all__ = [
    "REPLAY_POLICY_PROFILE_ID",
    "REPLAY_SCHEMA_VERSION",
    "REVIEW_DATA_CLASS",
    "REVIEW_STAGE",
    "PolicyGate",
    "ProposalOutcome",
    "ReviewTranslateError",
    "canonical_instruction",
    "declared_proposal_payload",
    "propose_review_command",
    "review_policy_gate",
]
