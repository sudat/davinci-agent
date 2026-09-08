"""Server-saved review proposal sets — the adoption authority (brief §5.3).

At CHAT time the cockpit persists every previewed draft set to the
episode's ``review-proposals.jsonl``; the apply route authorizes a request
only by FULL field-for-field equality against one unconsumed saved set
whose ``base_plan_version`` still matches the plan head. The browser's
echoed text or a recomputed hash alone is never adoption authority — a
fabricated draft (any mutated field) matches nothing and is a typed 422.
Runtime episode-dir state (like ``rebuild-requests.jsonl``), never an
authoritative artifact type.
"""

# allow: SIZE_OK — one adoption-authority concern per file (storage + the
# typed resolve guards over the same saved-set model); the 工程2 rework adds
# the ``proposal_kind`` field, ``effective_proposal_kind``, and the per-kind
# apply-route guards (_require_kind_route), which ARE this module's subject.

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, ValidationError

from services.contracts.primitives import StrictModel
from services.episode_cockpit.errors import CockpitConflictError
from services.episode_cockpit.models import (
    NonEmpty,
    ProposalKind,
    ProposalOutcome,
    ReviewProposalConsumed,
    ReviewReactionKind,
    SequenceNumber,
)
from services.episode_cockpit.review_chat import (
    ReviewChatContext,
    ReviewChatError,
    ReviewCommandDraft,
    interpret_command,
)
from services.foundation_io import canonical_model_bytes

REVIEW_PROPOSALS_NAME = "review-proposals.jsonl"
REVIEW_PROPOSALS_CONSUMED_NAME = "review-proposals-consumed.jsonl"

DEFAULT_PROPOSAL_KIND: ProposalKind = "command-bundle"

type ProposalDraftSequence = Annotated[
    tuple[ReviewCommandDraft, ...], BeforeValidator(tuple)
]


class ReviewProposalSet(StrictModel):
    """One chat message's previewed proposals (server authority to apply).

    ``base_plan_version`` is the plan head string at PREVIEW time (U09
    guard): applying while the head moved is a typed 409
    ``proposal-stale``. ``None`` means the preview ran before any plan
    existed; such a set stays applicable only while the head is still the
    untouched bootstrap (v1).

    工程2 (additive, backward compatible): ``responds_to_set`` /
    ``reaction_kind`` link a reaction-driven set to the earlier set it
    answers (rejection re-investigation, continuation adjustment).

    工程2 rework: ``proposal_kind`` separates 「一案の中の複数修正」
    (``command-bundle`` — one plan fix, all drafts applied TOGETHER) from
    「互いに選択する代替案」 (``alternatives`` — mutually exclusive, exactly
    ONE adopted). ``None``/absent (old saved sets) IS a command bundle
    (``effective_proposal_kind``).
    """

    schema_version: Literal["cockpit-review-proposals-v1"] = (
        "cockpit-review-proposals-v1"
    )
    sequence: SequenceNumber
    chat_sequence: SequenceNumber
    base_plan_version: NonEmpty | None = None
    drafts: ProposalDraftSequence
    created_at: NonEmpty
    responds_to_set: SequenceNumber | None = None
    reaction_kind: ReviewReactionKind | None = None
    proposal_kind: ProposalKind | None = None


def effective_proposal_kind(proposal_set: ReviewProposalSet) -> ProposalKind:
    """Old saved sets (field absent → None) are command bundles."""

    return proposal_set.proposal_kind or DEFAULT_PROPOSAL_KIND


def append_proposal_set(episode_dir: Path, proposal_set: ReviewProposalSet) -> None:
    """Append one previewed set (canonical bytes + newline; never rewrite)."""

    log_path = episode_dir / REVIEW_PROPOSALS_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write(canonical_model_bytes(proposal_set) + b"\n")


def load_proposal_sets(episode_dir: Path) -> list[ReviewProposalSet]:
    """Read back every saved set in order; absent file means none saved."""

    return _load_jsonl(
        episode_dir / REVIEW_PROPOSALS_NAME,
        ReviewProposalSet,
        "review-proposals-log-corrupt",
    )


def append_proposal_consumption(
    episode_dir: Path, entry: ReviewProposalConsumed
) -> None:
    """Append one consumption record AFTER a successful apply."""

    log_path = episode_dir / REVIEW_PROPOSALS_CONSUMED_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")


def load_consumed_proposals(episode_dir: Path) -> frozenset[int]:
    """Sequences of the proposal sets already consumed by an apply."""

    entries = _load_jsonl(
        episode_dir / REVIEW_PROPOSALS_CONSUMED_NAME,
        ReviewProposalConsumed,
        "review-proposals-consumed-log-corrupt",
    )
    return frozenset(entry.set_sequence for entry in entries)


def latest_unconsumed_set(
    sets: list[ReviewProposalSet], consumed: frozenset[int]
) -> ReviewProposalSet | None:
    """The most recent set with no consumption record (any outcome consumes)."""

    unconsumed = [saved for saved in sets if saved.sequence not in consumed]
    return unconsumed[-1] if unconsumed else None


def proposal_content(
    drafts: tuple[ReviewCommandDraft, ...] | list[ReviewCommandDraft],
) -> list[tuple[str, float | None, float | None]]:
    """Edit-content signature of the CONCRETE proposals (kind-None flagged
    drafts are not proposals); text/command_id intentionally excluded — the
    reacting message's wording always differs."""

    return [
        (draft.command_kind, draft.target_seconds, draft.seconds_delta)
        for draft in drafts
        if draft.command_kind is not None
    ]


def repeats_rejected_set(
    new_drafts: list[ReviewCommandDraft],
    rejected_drafts: tuple[ReviewCommandDraft, ...],
) -> bool:
    """U05/V5-RSL-003 guard: the new set would re-present the rejected
    proposals unchanged (同じ案を自動保存/再提示しない)."""

    new_content = proposal_content(new_drafts)
    return bool(new_content) and new_content == proposal_content(rejected_drafts)


def adoption_outcome(
    resolved: tuple[ReviewCommandDraft, ...], applied_set: ReviewProposalSet
) -> ProposalOutcome:
    """`applied` = the whole set; `chosen` = one draft of a multi-draft set
    (U04 single-draft adoption path)."""

    return "applied" if tuple(resolved) == applied_set.drafts else "chosen"


def _load_jsonl(path: Path, model: type, code: str) -> list:
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(model.model_validate_json(line))
        except ValidationError as error:
            raise ReviewChatError(code, f"unparsable line in {path}") from error
    return entries


def _is_single_choice(
    resolved: tuple[ReviewCommandDraft, ...], found: ReviewProposalSet
) -> bool:
    """U04: one echoed draft that IS one draft of a saved MULTI-draft set —
    the adoption path for 「Bが好き」 (recorded outcome=chosen at apply)."""

    return (
        len(resolved) == 1 and len(found.drafts) > 1 and resolved[0] in found.drafts
    )


def _require_kind_route(
    resolved: tuple[ReviewCommandDraft, ...], found: ReviewProposalSet
) -> None:
    """工程2 rework: the set's ``proposal_kind`` routes HOW it may be adopted.

    A command bundle is one plan fix previewed as several commands: adopting
    a single draft would silently drop the rest (``bundle-requires-full-
    apply``) — sequential single adoption is NOT an equivalent, because the
    first adoption consumes the set and moves the base. Alternatives are
    mutually exclusive: echoing all of them would apply contradictory
    proposals together (``alternatives-require-choice``); adopt ONE.
    """

    kind = effective_proposal_kind(found)
    if kind == "command-bundle" and _is_single_choice(resolved, found):
        raise ReviewChatError(
            "bundle-requires-full-apply",
            "this previewed set is one command bundle of several fixes; "
            "apply all of its drafts together — adopting one would silently "
            "drop the rest",
        )
    if (
        kind == "alternatives"
        and len(found.drafts) > 1
        and tuple(resolved) == found.drafts
    ):
        raise ReviewChatError(
            "alternatives-require-choice",
            "these drafts are mutually exclusive alternatives; adopt ONE of "
            "them (echo just that draft) instead of applying all",
        )


def resolve_authoritative_drafts(  # noqa: PLR0913 (the seven authorization inputs ARE the contract — sets, consumption, head and request must stay individually explicit at the security boundary)
    *,
    text: str,
    at_seconds: float | None,
    drafts: tuple[ReviewCommandDraft, ...] | None,
    sequence: int | None,
    sets: list[ReviewProposalSet],
    consumed: frozenset[int],
    head_version: int | None,
) -> tuple[tuple[ReviewCommandDraft, ...], ReviewProposalSet]:
    """Authorize an apply against the saved sets; the set comes back too.

    ``sequence`` (the review-chat response's sequence) names the set
    directly; without it the request matches by content — echoed drafts by
    full field-for-field equality, an old-client ``drafts=None`` by
    deterministic re-derivation, and (工程2 U04) a SINGLE echoed draft that
    field-for-field equals one draft of a saved multi-draft set. Any
    mutated field, a never-previewed text, or a consumed set is a typed
    422; a moved plan head is a 409.
    """

    if sequence is not None:
        named = [saved for saved in sets if saved.chat_sequence == sequence]
        if not named:
            raise ReviewChatError(
                "proposal-not-found",
                f"no previewed proposal set for chat sequence {sequence}",
            )
        found = named[-1]
        if found.sequence in consumed:
            raise ReviewChatError(
                "proposal-consumed",
                f"proposal set {found.sequence} was already applied; preview again",
            )
        if drafts is None:
            resolved = found.drafts
        else:
            resolved = tuple(drafts)
            if resolved != found.drafts and not _is_single_choice(resolved, found):
                raise ReviewChatError(
                    "proposal-mismatch",
                    "echoed drafts differ from the server-saved set for chat "
                    f"sequence {sequence}; re-preview the correction",
                )
        if not resolved or resolved[0].text != text:
            raise ReviewChatError(
                "proposal-mismatch",
                "request text differs from the saved proposal set",
            )
        _require_kind_route(resolved, found)
        _require_fresh_base(found, head_version)
        return resolved, found

    reference = (
        drafts
        if drafts is not None
        else (interpret_command(text, ReviewChatContext(at_seconds=at_seconds)),)
    )
    candidates = [saved for saved in sets if any(d.text == text for d in saved.drafts)]
    if not candidates:
        raise ReviewChatError(
            "proposal-not-found",
            "no previewed proposal set for this text; send review-chat first",
        )
    matching = [
        saved
        for saved in candidates
        if saved.drafts == reference or _is_single_choice(reference, saved)
    ]
    if not matching:
        raise ReviewChatError(
            "proposal-mismatch",
            "drafts differ from every server-saved set for this text; "
            "only previewed drafts may be applied",
        )
    unconsumed = [saved for saved in matching if saved.sequence not in consumed]
    if not unconsumed:
        raise ReviewChatError(
            "proposal-consumed",
            "the previewed proposal set was already applied; preview again",
        )
    found = unconsumed[-1]
    _require_kind_route(reference, found)
    _require_fresh_base(found, head_version)
    return reference, found


def _require_fresh_base(found: ReviewProposalSet, head_version: int | None) -> None:
    """U09: never apply a set previewed against a different plan head."""

    if found.base_plan_version is None:
        fresh = head_version in (None, 1)
    else:
        fresh = head_version == int(found.base_plan_version[1:])
    if not fresh:
        raise CockpitConflictError(
            "proposal-stale",
            f"the plan changed after this preview (previewed at "
            f"{found.base_plan_version or 'no plan'}, current head is "
            f"{'v' + str(head_version) if head_version is not None else 'no plan'}); "
            "re-preview the correction",
        )


__all__ = [
    "DEFAULT_PROPOSAL_KIND",
    "REVIEW_PROPOSALS_CONSUMED_NAME",
    "REVIEW_PROPOSALS_NAME",
    "ReviewProposalConsumed",
    "ReviewProposalSet",
    "adoption_outcome",
    "append_proposal_consumption",
    "append_proposal_set",
    "effective_proposal_kind",
    "latest_unconsumed_set",
    "load_consumed_proposals",
    "load_proposal_sets",
    "proposal_content",
    "repeats_rejected_set",
    "resolve_authoritative_drafts",
]
