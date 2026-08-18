"""Free-form review instruction translation for REAL bundles (Todo 46).

The owner's natural-language correction (Japanese or English) is parsed by
the DETERMINISTIC 0C classifier rules — a thin pattern parser (segment
removal, subtitle text correction, item subtitle correction, span
adjustment) producing a ``ReviewCommandProposal0C``, then classified
clear/ambiguous/conflict against the REAL bundle's committed plan via the
Todo-28 validator (two viable targets → ambiguous; unknown target → typed
unresolved error). An instruction matching no pattern is a typed
``unparsed_instruction`` refusal, never a coerced proposal. The Todo-12
policy gate is the same one the replay translator applies.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter

from services.cli.review_replay import (
    PlanView,
    ProposalOutcome,
    review_policy_gate,
)
from services.contracts.primitives import Identifier, Sha256
from services.foundation_io import canonical_model_bytes
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

FREEFORM_PROFILE_ID: Final = "phase-0c-deterministic-classifier-v1"
FREEFORM_SCHEMA_VERSION: Final = "preview-review-v1"

_REMOVE_JA: Final = re.compile(
    r"^セグメント\s+(?P<ids>[A-Za-z0-9ー〜、と\s]+?)を削除(?:してください)?。?$"
)
_REMOVE_EN: Final = re.compile(
    r"^remove\s+(?:segment\s+)?(?P<ids>[A-Za-z0-9,\s]+?)\.?$", re.IGNORECASE
)
_SPAN_JA: Final = re.compile(
    r"^セグメント\s+(?P<id>[a-z][0-9]+)の尺を\s*(?P<start>[0-9]+)〜(?P<end>[0-9]+)\s*"
    r"フレームに調整(?:してください)?。?$"
)
_SUBTITLE_TEXT_JA: Final = re.compile(
    r"^「(?P<old>[^「」]+)」という字幕を「(?P<new>[^「」]+)」に(?:修正|変更)(?:してください)?。?$"
)
_SUBTITLE_ITEM_JA: Final = re.compile(
    r"^字幕\s+(?P<id>[a-z][0-9]+)\s*を「(?P<new>[^「」]+)」に(?:修正|変更)(?:してください)?。?$"
)
_SUBTITLE_EN: Final = re.compile(
    r'^change\s+(?:subtitle\s+)?"(?P<old>[^"]+)"\s+to\s+"(?P<new>[^"]+)"\.?$', re.IGNORECASE
)
_ID_PATTERN: Final = re.compile(r"^[a-z][0-9]+$")
_ID_SEPARATOR: Final = re.compile(r"[、と,]|and|\s+")


class FreeformParseError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


_PROPOSAL_ADAPTER: TypeAdapter[ReviewCommandProposal0C] = TypeAdapter(
    ReviewCommandProposal0C
)
_IDENTIFIER: TypeAdapter[Identifier] = TypeAdapter(Identifier)


def _ids(raw: str) -> tuple[str, ...]:
    parts = [part for part in _ID_SEPARATOR.split(raw.strip()) if part.strip()]
    ids = [part.strip() for part in parts]
    for item in ids:
        if not _ID_PATTERN.match(item):
            raise FreeformParseError(
                "unparsed_instruction",
                f"segment reference {item!r} is not a plan item id (sN)",
            )
    return tuple(dict.fromkeys(ids))


def _envelope(episode_id: str, instruction: str, sequence: int) -> dict[str, object]:
    return {
        "proposal_id": f"prop-{episode_id}-ff{sequence}",
        "base_plan_version": None,
        "actor_intent": "model",
        "sequence": sequence,
        "confidence": {"num": 9, "den": 10},
        "ambiguity": {"status": "clear", "reasons": []},
        "evidence": [instruction],
    }


def _subtitle_selector(old: str, subtitles: dict[str, str]) -> dict[str, object]:
    matches = [item_id for item_id, text in subtitles.items() if text == old]
    if len(matches) == 1:
        return {"kind": "item_id", "item_id": matches[0]}
    return {"kind": "subtitle_text_match", "text": old}


def parse_instruction(
    episode_id: str, instruction: str, base_version: str, subtitles: dict[str, str]
) -> dict[str, object]:
    """Parse one instruction into a proposal payload or raise typed error."""

    text = instruction.strip()
    sequence = int(base_version[1:])
    envelope = _envelope(episode_id, text, sequence) | {
        "base_plan_version": base_version,
    }
    if match := _REMOVE_JA.match(text) or _REMOVE_EN.match(text):
        ids = _ids(match.group("ids"))
        return envelope | {
            "command_kind": "remove_segment",
            "candidate_targets": [
                {"item_id": item, "evidence": text} for item in ids
            ],
        }
    if match := _SPAN_JA.match(text):
        return envelope | {
            "command_kind": "adjust_source_span",
            "target": {"kind": "item_id", "item_id": match.group("id")},
            "new_span": {
                "start_frame": int(match.group("start")),
                "end_frame": int(match.group("end")),
            },
        }
    if match := _SUBTITLE_TEXT_JA.match(text) or _SUBTITLE_EN.match(text):
        return envelope | {
            "command_kind": "correct_subtitle",
            "target": _subtitle_selector(match.group("old"), subtitles),
            "new_text": match.group("new"),
            "language": "ja",
        }
    if match := _SUBTITLE_ITEM_JA.match(text):
        return envelope | {
            "command_kind": "correct_subtitle",
            "target": {"kind": "item_id", "item_id": match.group("id")},
            "new_text": match.group("new"),
            "language": "ja",
        }
    raise FreeformParseError(
        "unparsed_instruction",
        "the instruction matches no free-form correction pattern (segment removal, "
        "subtitle correction, or span adjustment); refusing to coerce it",
    )


def propose_freeform(  # noqa: PLR0913 (translator adapter contract, mirrors the replay path)
    *,
    episode_id: str,
    instruction: str,
    current: PlanView,
    policy: ResolvedConfig,
    policy_sha: str,
    translator_sha: str,
) -> ProposalOutcome:
    """Translate one free-form instruction against the REAL bundle's plan."""

    gate = review_policy_gate(
        policy, episode_id=episode_id, policy_sha=policy_sha, translator_sha=translator_sha
    )
    text = instruction.strip()
    request = TranslatorRequest(
        episode_id=_IDENTIFIER.validate_python(episode_id),
        policy_profile_id=_IDENTIFIER.validate_python(FREEFORM_PROFILE_ID),
        schema_version=_IDENTIFIER.validate_python(FREEFORM_SCHEMA_VERSION),
        instruction=text,
        plan_context=plan_context_from_edit_plan(current.plan),
    )
    digest: Sha256 = request_hash(request)
    subtitles = {
        item.item_id: item.subtitle_text
        for item in current.plan.plan.items
        if item.kind == "subtitle" and item.subtitle_text is not None
    }
    sequence = int(current.version[1:])
    try:
        payload = parse_instruction(episode_id, text, current.version, subtitles)
    except FreeformParseError as error:
        return ProposalOutcome(
            schema_version="review-proposal-v1",
            episode_id=episode_id,
            instruction=text,
            status="error",
            error_code=error.code,
            error_detail=error.detail,
            request_hash=digest,
            policy=gate,
        )
    proposal = _PROPOSAL_ADAPTER.validate_python(payload)
    proposal_json = canonical_model_bytes(proposal).decode()
    try:
        outcome = validate_proposal(current.plan, proposal)
    except ProposalValidationError as error:
        return ProposalOutcome(
            schema_version="review-proposal-v1",
            episode_id=episode_id,
            instruction=text,
            status="error",
            error_code=error.code,
            error_detail=error.detail,
            request_hash=digest,
            policy=gate,
        )
    return ProposalOutcome(
        schema_version="review-proposal-v1",
        episode_id=episode_id,
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
        command_index=sequence,
    )


__all__ = ["FreeformParseError", "parse_instruction", "propose_freeform"]
