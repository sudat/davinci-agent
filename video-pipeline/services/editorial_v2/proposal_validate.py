# allow: SIZE_OK — task 29 pins the commit scope to this single module (no sibling
# files); single responsibility: moment-selection proposal validation + commit path.
"""Moment-selection proposal validation + commit path (task 29).

Validation re-queries the v2 media-query index instead of trusting the
proposal: every cited evidence ref must resolve through ``shot_detail``
(or be an id a real api_v2 row produced during evidence-bundle assembly),
and every source span must be covered by an indexed shot row. Invented
ids/spans raise ``HallucinatedReferenceError`` naming them. Lock checks
compare the incoming intent against the minimal committed lock registry
and refuse conflicting changes with ``LockConflictError`` (v1 taxonomy
style: typed errors carrying code/detail, cf. services/validate).

The commit path writes through the review_command store conventions: a
sealed JSONL event log (``append_events``/``load_events`` from
services/review_command/store.py) plus an immutable versions index with
a contiguous chain, using the additive ``moment-selection-v2-committed``
event kind. Re-committing the same proposal bytes is refused as a
duplicate; each commit mints the next version and returns a receipt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.editorial_v2.moment_models import MomentIntent  # noqa: TC001 (pydantic field)
from services.editorial_v2.removal_policy import ineligible_removal_detail
from services.foundation_io import atomic_write, canonical_model_bytes
from services.media_query import v2_models as vm
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    MOMENT_SELECTION_V2_COMMITTED,
    EventSeal,
    ReviewEvent0C,
    compute_event_id,
)
from services.review_command.store import append_events, load_events, seal_path

if TYPE_CHECKING:
    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.moment_models import MomentSelectionProposalV2
    from services.editorial_v2.removal_policy import RemovalEligibilityV1
    from services.media_query.query_v2 import MediaQueryApiV2

INDEX_NAME = "versions.json"
LOG_NAME = "events.jsonl"


class MomentValidationError(Exception):
    """Validation refusal base (code/detail style, cf. ReviewCommitError)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class HallucinatedReferenceError(MomentValidationError):
    """A cited id/span no api_v2 row produces; the id is named on the error."""

    def __init__(self, reference: str, detail: str) -> None:
        super().__init__("hallucinated-reference", detail)
        self.reference = reference


class EvidenceCoverageError(MomentValidationError):
    """The evidence bundle does not cover the proposal as claimed."""


class RemovalEligibilityError(MomentValidationError):
    """A remove lacks precomputed deterministic eligibility (T4 fail-closed)."""


class LockConflictError(MomentValidationError):
    """An incoming intent contradicts the committed lock of a candidate."""

    def __init__(
        self, candidate_id: str, locked_intent: MomentIntent, incoming_intent: MomentIntent
    ) -> None:
        super().__init__(
            "lock-conflict",
            f"candidate {candidate_id} is locked with intent {locked_intent}; the "
            f"incoming proposal changes it to {incoming_intent} — refused as conflict",
        )
        self.candidate_id = candidate_id
        self.locked_intent = locked_intent
        self.incoming_intent = incoming_intent


class MomentCommitError(Exception):
    """Commit-path refusal base (code/detail style)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class DuplicateCommitError(MomentCommitError):
    """The same proposal bytes were already committed; refused, not re-run."""

    def __init__(self, proposal_sha256: str, version: str | None) -> None:
        super().__init__(
            "duplicate-commit",
            f"proposal {proposal_sha256} is already committed as {version}; "
            "re-committing identical bytes is refused",
        )
        self.proposal_sha256 = proposal_sha256


class ValidationMismatchError(MomentCommitError):
    """The validation receipt does not bind to the proposal being committed."""


class EpisodeMismatchError(MomentCommitError):
    """The proposal targets a different episode than the store."""


class MomentLockRecord(StrictModel):
    """Minimal committed lock: one candidate's intent frozen by a reviewer."""

    candidate_id: Identifier
    locked_intent: MomentIntent
    locked_by: Identifier
    locked_at: str = Field(min_length=1, strict=True)


class MomentSelectionVersionEntry(StrictModel):
    """One committed version: proposal sha, commit event, parent version."""

    proposal_sha256: Sha256 | None = None
    event_id: Sha256 | None = None
    parent_version: int | None = Field(default=None, ge=1, strict=True)


class MomentSelectionVersionsIndex(StrictModel):
    schema_version: Literal["moment-selection-versions-v1"]
    episode_id: Identifier
    versions: dict[str, MomentSelectionVersionEntry]

    @model_validator(mode="after")
    def require_contiguous_chain(self) -> MomentSelectionVersionsIndex:
        for position in range(1, len(self.versions) + 1):
            entry = self.versions.get(str(position))
            parent = None if position == 1 else position - 1
            if entry is None or entry.parent_version != parent:
                raise PydanticCustomError(
                    "versions", "version chain broken at {position}", {"position": position}
                )
            genesis = entry.proposal_sha256 is None and entry.event_id is None
            if (position == 1) != genesis:
                raise PydanticCustomError(
                    "versions",
                    "version {position} has a broken genesis binding",
                    {"position": position},
                )
        return self


@dataclass(frozen=True, slots=True)
class MomentSelectionStore:
    """Directory wiring for the sealed moment-selection log + versions index."""

    plan_dir: Path

    @property
    def log_path(self) -> Path:
        return self.plan_dir / LOG_NAME

    @property
    def index_path(self) -> Path:
        return self.plan_dir / INDEX_NAME


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Receipt of a passed validation — failures raise typed errors."""

    proposal_sha256: str
    episode_id: str
    candidates_checked: int
    refs_verified: int
    locks_checked: int
    ok: Literal[True] = True


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """Successful commit: the minted version plus its sealed event."""

    episode_id: str
    version: int
    base_version: str
    proposal_sha256: str
    event_id: str
    sequence: int


def load_moment_index(store: MomentSelectionStore) -> MomentSelectionVersionsIndex:
    try:
        return MomentSelectionVersionsIndex.model_validate_json(store.index_path.read_bytes())
    except OSError as error:
        raise MomentCommitError("store_not_initialized", str(error)) from error
    except ValidationError as error:
        raise MomentCommitError("invalid_index", str(error)) from error


def initialize_moment_store(store: MomentSelectionStore, *, episode_id: str) -> None:
    """Register the genesis version (v1) of a store; idempotent per episode."""

    store.plan_dir.mkdir(parents=True, exist_ok=True)
    if store.index_path.exists():
        index = load_moment_index(store)
        if index.episode_id == episode_id:
            return
        raise MomentCommitError(
            "store_init_conflict", "store already initialized for a different episode"
        )
    atomic_write(store.log_path, b"")
    atomic_write(
        seal_path(store.log_path),
        canonical_model_bytes(EventSeal(sequence=0, event_id=GENESIS_EVENT_HASH)),
    )
    atomic_write(
        store.index_path,
        canonical_model_bytes(
            MomentSelectionVersionsIndex(
                schema_version="moment-selection-versions-v1",
                episode_id=episode_id,
                versions={"1": MomentSelectionVersionEntry()},
            )
        ),
    )


def _check_bundle_coverage(proposal: MomentSelectionProposalV2, bundle: EvidenceBundleV2) -> None:
    seen: set[str] = set()
    covered = {entry.candidate_id for entry in bundle.entries}
    for candidate in proposal.candidates:
        if candidate.candidate_id in seen:
            raise EvidenceCoverageError(
                "duplicate-candidate",
                f"candidate {candidate.candidate_id} appears more than once",
            )
        seen.add(candidate.candidate_id)
        if candidate.candidate_id not in covered:
            raise EvidenceCoverageError(
                "bundle-gap",
                f"candidate {candidate.candidate_id} has no corroborating evidence bundle entry",
            )


def _check_against_index(
    api_v2: MediaQueryApiV2, proposal: MomentSelectionProposalV2, known_refs: frozenset[str]
) -> tuple[str, ...]:
    """Re-query every cited ref and span; invented ids/spans are refused."""
    verified: set[str] = set()
    pagination = vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=0)
    for candidate in proposal.candidates:
        span = vm.FrameSpan(
            start_frame=candidate.source_span.start_frame,
            end_frame=candidate.source_span.end_frame,
        )
        rows = api_v2.shots(vm.ShotsRequest(span=span, pagination=pagination)).rows
        covered = any(
            row.span.start_frame <= span.start_frame and row.span.end_frame >= span.end_frame
            for row in rows
        )
        if not covered:
            raise HallucinatedReferenceError(
                candidate.candidate_id,
                f"span [{span.start_frame},{span.end_frame}) of candidate "
                f"{candidate.candidate_id} is not covered by any indexed shot row",
            )
        for ref in candidate.evidence_refs:
            if ref in verified:
                continue
            try:
                api_v2.shot_detail(vm.ShotDetailRequest(shot_id=ref))
            except vm.ApiShotNotFoundV2:
                if ref not in known_refs:
                    raise HallucinatedReferenceError(
                        ref,
                        f"candidate {candidate.candidate_id} cites evidence ref "
                        f"{ref!r} that no api_v2 row produced and no bundle entry "
                        "recorded",
                    ) from None
            verified.add(ref)
    return tuple(sorted(verified))


def _check_locks(proposal: MomentSelectionProposalV2, locks: tuple[MomentLockRecord, ...]) -> None:
    by_id = {candidate.candidate_id: candidate for candidate in proposal.candidates}
    for lock in locks:
        candidate = by_id.get(lock.candidate_id)
        if candidate is not None and candidate.intent != lock.locked_intent:
            raise LockConflictError(lock.candidate_id, lock.locked_intent, candidate.intent)


def validate_proposal(
    proposal: MomentSelectionProposalV2,
    api_v2: MediaQueryApiV2,
    evidence_bundle: EvidenceBundleV2,
    *,
    locks: tuple[MomentLockRecord, ...] = (),
    removal_eligibility: tuple[RemovalEligibilityV1, ...] | None = None,
) -> ValidationResult:
    """Validate a moment-selection proposal against re-queried ground truth.

    Order: bundle coverage (semantic) → removal policy (T4, when the caller
    supplies the runtime-only eligibility) → hallucinated id/span re-query →
    lock conflict. Any refusal raises a typed error; the returned receipt
    binds the validation to the exact proposal bytes via its sha256.
    """

    digest = hashlib.sha256(canonical_model_bytes(proposal)).hexdigest()
    _check_bundle_coverage(proposal, evidence_bundle)
    if removal_eligibility is not None:
        _check_removal_policy(proposal, removal_eligibility)
    known_refs = frozenset(ref for entry in evidence_bundle.entries for ref in entry.evidence_refs)
    refs = _check_against_index(api_v2, proposal, known_refs)
    _check_locks(proposal, locks)
    return ValidationResult(
        proposal_sha256=digest,
        episode_id=proposal.episode_id,
        candidates_checked=len(proposal.candidates),
        refs_verified=len(refs),
        locks_checked=len(locks),
    )


def _check_removal_policy(
    proposal: MomentSelectionProposalV2, eligibility: tuple[RemovalEligibilityV1, ...]
) -> None:
    detail = ineligible_removal_detail(
        proposal.candidates, {entry.candidate_id: entry for entry in eligibility}
    )
    if detail is not None:
        raise RemovalEligibilityError("removal-not-eligible", detail)


def _build_commit_event(
    proposal: MomentSelectionProposalV2,
    *,
    sequence: int,
    base_plan_version: str,
    result_plan_version: str,
    previous_event_hash: str,
) -> ReviewEvent0C:
    proposal_json = canonical_model_bytes(proposal).decode()
    draft = ReviewEvent0C(
        event_id=GENESIS_EVENT_HASH,
        sequence=sequence,
        kind=MOMENT_SELECTION_V2_COMMITTED,
        proposal_json=proposal_json,
        proposal_sha256=hashlib.sha256(proposal_json.encode()).hexdigest(),
        base_plan_version=base_plan_version,
        result_plan_version=result_plan_version,
        applied=True,
        actor_intent="model",
        previous_event_hash=previous_event_hash,
    )
    return draft.model_copy(update={"event_id": compute_event_id(draft)})


def commit_selection(
    proposal: MomentSelectionProposalV2,
    validation: ValidationResult,
    *,
    store: MomentSelectionStore,
) -> CommitReceipt:
    """Commit a validated proposal as the next sealed version (idempotent-refusing)."""

    digest = hashlib.sha256(canonical_model_bytes(proposal)).hexdigest()
    if digest != validation.proposal_sha256:
        raise ValidationMismatchError(
            "validation-mismatch",
            "the validation receipt binds proposal bytes "
            f"{validation.proposal_sha256[:8]}…, not the proposal being committed "
            f"({digest[:8]}…)",
        )
    index = load_moment_index(store)
    if proposal.episode_id != index.episode_id:
        raise EpisodeMismatchError(
            "episode-mismatch",
            f"proposal episode {proposal.episode_id} is not the store episode {index.episode_id}",
        )
    events = load_events(store.log_path)
    for event in events:
        if event.kind == MOMENT_SELECTION_V2_COMMITTED and event.proposal_sha256 == digest:
            raise DuplicateCommitError(digest, event.result_plan_version)
    latest = len(index.versions)
    version = latest + 1
    sequence = events[-1].sequence + 1 if events else 1
    previous_hash = events[-1].event_id if events else GENESIS_EVENT_HASH
    event = _build_commit_event(
        proposal,
        sequence=sequence,
        base_plan_version=f"v{latest}",
        result_plan_version=f"v{version}",
        previous_event_hash=previous_hash,
    )
    append_events(store.log_path, (event,))
    atomic_write(
        store.index_path,
        canonical_model_bytes(
            MomentSelectionVersionsIndex(
                schema_version=index.schema_version,
                episode_id=index.episode_id,
                versions=index.versions
                | {
                    str(version): MomentSelectionVersionEntry(
                        proposal_sha256=digest, event_id=event.event_id, parent_version=latest
                    )
                },
            )
        ),
    )
    return CommitReceipt(
        episode_id=proposal.episode_id,
        version=version,
        base_version=f"v{latest}",
        proposal_sha256=digest,
        event_id=event.event_id,
        sequence=sequence,
    )


__all__ = [
    "CommitReceipt",
    "DuplicateCommitError",
    "EpisodeMismatchError",
    "EvidenceCoverageError",
    "HallucinatedReferenceError",
    "LockConflictError",
    "MomentCommitError",
    "MomentLockRecord",
    "MomentSelectionStore",
    "MomentSelectionVersionEntry",
    "MomentSelectionVersionsIndex",
    "MomentValidationError",
    "RemovalEligibilityError",
    "ValidationMismatchError",
    "ValidationResult",
    "commit_selection",
    "initialize_moment_store",
    "load_moment_index",
    "validate_proposal",
]
