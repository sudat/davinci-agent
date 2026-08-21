"""PRESENTATION_APPROVED operation records (Todo 61): binding + invalidation.

A presentation approval is its OWN local TTY operation record following
the Todo-13 chain: purpose ``presentation``, target type
``presentation-bundle``, bound to the Preview artifact sha AND the
Presentation Manifest sha (plus the editorial fingerprint captured at
approval time). Supersession is ENFORCED here in code: any
manifest/profile/asset hash drift supersedes the presentation approval,
and editorial meaning/timing drift ALSO supersedes EDITORIAL_APPROVED.
Purpose isolation is typed: reusing an Editorial or Final record for the
presentation purpose is a ``purpose_reuse`` refusal, never an
authorization.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal

from services.approvals.ingress import record_fixture_operation
from services.approvals.verify import (
    AuthorizationVerdict,
    evaluate_authorization,
    validate_supersession_chain,
)
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import canonical_json_bytes

if TYPE_CHECKING:
    from services.approvals.models import (
        ApprovalPurpose,
        ChainedOperationRecord,
        OperationDraft,
        RecordDecision,
    )
    from services.approvals.store import OperationRecordStore
    from services.presentation.manifest import PresentationManifest

type SupersessionCause = Literal[
    "manifest_drift",
    "profile_drift",
    "asset_drift",
    "editorial_semantic_drift",
]

REFUSAL_PURPOSE_REUSE = "purpose_reuse"


class PresentationApprovalError(Exception):
    """Typed presentation-approval failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class PresentationApprovalBinding(StrictModel):
    """The artifact hashes a PRESENTATION_APPROVED record is bound to."""

    schema_version: Literal["presentation-approval-binding-v1"] = (
        "presentation-approval-binding-v1"
    )
    episode_id: Identifier
    preview_artifact_sha256: Sha256
    presentation_manifest_sha256: Sha256
    editorial_fingerprint_sha256: Sha256

    def bundle_hash(self) -> Sha256:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def bind_presentation_approval(
    manifest: PresentationManifest, *, preview_artifact_sha256: Sha256
) -> PresentationApprovalBinding:
    """Bind an approval to (Preview artifact, Presentation Manifest)."""

    return PresentationApprovalBinding(
        episode_id=manifest.episode_id,
        preview_artifact_sha256=preview_artifact_sha256,
        presentation_manifest_sha256=manifest.manifest_sha256,
        editorial_fingerprint_sha256=manifest.editorial_fingerprint,
    )


def presentation_record_draft(
    binding: PresentationApprovalBinding,
    *,
    actor_id: str,
    decision: RecordDecision = "approve",
    wall_time_unix: int | None = None,
) -> OperationDraft:
    """The fixture-marked draft; operator records use the Todo-13 TTY ingress
    with the SAME purpose and ``target_bundle_hash=binding.bundle_hash()``."""

    return record_fixture_operation(
        purpose="presentation",
        target_bundle_hash=binding.bundle_hash(),
        decision=decision,
        actor_id=actor_id,
        wall_time_unix=wall_time_unix,
    )


def record_presentation_approval(
    store: OperationRecordStore,
    binding: PresentationApprovalBinding,
    *,
    actor_id: str,
    decision: RecordDecision = "approve",
) -> ChainedOperationRecord:
    return store.append(presentation_record_draft(binding, actor_id=actor_id, decision=decision))


class SupersessionVerdict(StrictModel):
    """Which approvals a drift invalidates; causes are typed."""

    presentation_superseded: bool
    editorial_superseded: bool
    causes: tuple[SupersessionCause, ...] = ()


def _verdict(causes: tuple[SupersessionCause, ...]) -> SupersessionVerdict:
    return SupersessionVerdict(
        presentation_superseded=bool(causes),
        editorial_superseded="editorial_semantic_drift" in causes,
        causes=causes,
    )


def evaluate_supersession(
    approved: PresentationManifest, current: PresentationManifest
) -> SupersessionVerdict:
    """Manifest-level drift: typed causes from the declared hash surface."""

    causes: list[SupersessionCause] = []
    if approved.profile_snapshot_sha256 != current.profile_snapshot_sha256:
        causes.append("profile_drift")
    if approved.registry_snapshot_sha256 != current.registry_snapshot_sha256 or (
        approved.assets != current.assets
    ):
        causes.append("asset_drift")
    if approved.editorial_fingerprint != current.editorial_fingerprint:
        causes.append("editorial_semantic_drift")
    if approved.manifest_sha256 != current.manifest_sha256:
        causes.append("manifest_drift")
    return _verdict(tuple(causes))


def evaluate_binding_supersession(
    binding: PresentationApprovalBinding, current: PresentationManifest
) -> SupersessionVerdict:
    """Post-approval drift from the bound hashes alone (no old manifest)."""

    causes: list[SupersessionCause] = []
    if binding.editorial_fingerprint_sha256 != current.editorial_fingerprint:
        causes.append("editorial_semantic_drift")
    if (
        binding.presentation_manifest_sha256 != current.manifest_sha256
        or binding.editorial_fingerprint_sha256 != current.editorial_fingerprint
    ):
        causes.append("manifest_drift")
    return _verdict(tuple(causes))


def enforce_supersession(verdict: SupersessionVerdict) -> None:
    """The code-enforced gate: stale approvals must never authorize builds."""

    if verdict.editorial_superseded:
        raise PresentationApprovalError(
            "superseded_editorial",
            "editorial meaning/timing drifted; PRESENTATION_APPROVED and "
            "EDITORIAL_APPROVED are both superseded and must be recorded fresh",
        )
    if verdict.presentation_superseded:
        raise PresentationApprovalError(
            "superseded_presentation",
            "manifest/profile/asset drift supersedes PRESENTATION_APPROVED; "
            "record a fresh presentation approval for the new hashes",
        )


def authorize_presentation(
    records: tuple[ChainedOperationRecord, ...],
    binding: PresentationApprovalBinding,
    *,
    chain_key: bytes,
    operator_gate: bool = False,
) -> AuthorizationVerdict:
    """Authorize with typed purpose isolation (``purpose_reuse`` refusal).

    The boundary is chain-verified first: a record set whose keyed hash
    chain does not verify under ``chain_key`` can never authorize a
    presentation build, regardless of its semantics.
    """

    try:
        validate_supersession_chain(records, chain_key=chain_key)
    except ValueError as error:
        raise PresentationApprovalError(
            "records-chain-invalid",
            f"the operation-record chain does not verify under the store key: {error}",
        ) from error
    target_hash = binding.bundle_hash()
    reused = [
        record
        for record in records
        if record.target_hash == target_hash and record.purpose != "presentation"
    ]
    if reused:
        record = reused[-1]
        return AuthorizationVerdict(
            authorized=False,
            refusal_code=REFUSAL_PURPOSE_REUSE,
            record_id=record.record_id,
            purpose=record.purpose,
            target_hash=target_hash,
        )
    return evaluate_authorization(
        records,
        purpose="presentation",
        target_hash=target_hash,
        target_type="presentation-bundle",
        operator_gate=operator_gate,
    )


def other_purpose_for_target(
    records: tuple[ChainedOperationRecord, ...], target_hash: str
) -> ApprovalPurpose | None:
    """The reused purpose, if an Editorial/Final record claims this target."""

    for record in records:
        if record.target_hash == target_hash and record.purpose != "presentation":
            return record.purpose
    return None


__all__ = [
    "REFUSAL_PURPOSE_REUSE",
    "PresentationApprovalBinding",
    "PresentationApprovalError",
    "SupersessionCause",
    "SupersessionVerdict",
    "authorize_presentation",
    "bind_presentation_approval",
    "enforce_supersession",
    "evaluate_binding_supersession",
    "evaluate_supersession",
    "other_purpose_for_target",
    "presentation_record_draft",
    "record_presentation_approval",
]
