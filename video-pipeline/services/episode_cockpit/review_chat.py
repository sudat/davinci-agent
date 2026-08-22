"""Task 47: review chat -> structured command -> partial rebuild (PRD 13.3).

Deterministic (no LLM), Japanese-first interpretation. Materially ambiguous
input surfaces an interpretation preview flagged ``needs_confirmation`` —
never a guess. Approved drafts go through the REAL ``review_command``
conventions where the Phase-0C proposal contract has a span-level
translation (remove / keep-longer / quiet-longer -> remove_segment /
adjust_source_span via ``commit_command``: sealed log + immutable versions);
other domains become AppliedCommands recorded in the episode's
``applied-commands.jsonl`` — the audit entry every rebuild plan derives
from. Chat text is DATA: only patterns are matched, nothing is executed.
"""

# allow: SIZE_OK — 474 pure LOC under a plan-pinned single-file commit scope
# (task 47: review_chat.py only); ~120 LOC are the pure command-pattern data
# table plus immutable store/lineage tables. Split parser-vs-apply/plan into
# two modules at task-51 wiring when models.py unlocks.

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.edit_plan_0c import EditPlan0C, EditPlanItem0C, ItemIdSelector0C
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import NonEmpty, Seconds  # noqa: TC001 (pydantic runtime)
from services.foundation_io import canonical_model_bytes
from services.review_command.commit import CommitOutcome, commit_command
from services.review_command.models import (
    AdjustSourceSpanProposal0C,
    CandidateTarget0C,
    Confidence0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
    SpanBounds0C,
)
from services.review_command.store import HeadState, OperatorDecision0C, load_head

type ReviewCommandKind = Literal[
    "remove_section",
    "keep_longer",
    "use_other_take",
    "insert_broll",
    "mark_boring",
    "quiet_longer",
    "subtitle_shorter",
    "remove_effect",
    "lower_bgm",
    "match_color",
    "channel_lower_third",
    "episode_only",
]
type AffectedDomain = Literal["selection", "edit_plan", "presentation", "scope"]
type StageLineage = Mapping[AffectedDomain, frozenset[str]]
type StageSequence = Annotated[tuple[str, ...], BeforeValidator(tuple)]
type SpanOperation = Literal["remove_segment", "adjust_source_span"]

PIPELINE_STAGES: tuple[str, ...] = (
    "ingest",
    "normalize",
    "analyze",
    "selection",
    "plan",
    "compile",
    "preview",
    "resolve_build",
    "qc",
    "render",
    "publish",
)
COMMAND_DOMAIN: Mapping[ReviewCommandKind, AffectedDomain] = {
    "remove_section": "edit_plan",
    "keep_longer": "edit_plan",
    "quiet_longer": "edit_plan",
    "use_other_take": "selection",
    "insert_broll": "selection",
    "mark_boring": "selection",
    "subtitle_shorter": "presentation",
    "remove_effect": "presentation",
    "lower_bgm": "presentation",
    "match_color": "presentation",
    "channel_lower_third": "presentation",
    "episode_only": "scope",
}
DEFAULT_LINEAGE: StageLineage = {
    "selection": frozenset(
        {"selection", "plan", "compile", "preview", "resolve_build", "qc", "render"}
    ),
    "edit_plan": frozenset({"plan", "compile", "preview", "resolve_build", "qc", "render"}),
    "presentation": frozenset({"compile", "preview", "resolve_build", "qc", "render"}),
    "scope": frozenset(),
}
APPLIED_COMMANDS_NAME = "applied-commands.jsonl"


class ReviewChatError(CockpitUnprocessableError):
    """Typed review-chat parse/apply failure (structured 422 at the boundary)."""


class ReviewChatContext(StrictModel):
    """Where the reviewer was when the message was sent (player position)."""

    at_seconds: Seconds | None = None


class ReviewCommandDraft(StrictModel):
    """Deterministic interpretation preview of one natural-language message."""

    schema_version: Literal["cockpit-review-command-draft-v1"] = (
        "cockpit-review-command-draft-v1"
    )
    command_id: Identifier
    command_kind: ReviewCommandKind | None
    text: NonEmpty
    target_seconds: Seconds | None = None
    seconds_delta: Seconds | None = None
    scope: Literal["episode", "channel"] = "episode"
    needs_confirmation: bool
    confirmation_reason: str | None = None

    @model_validator(mode="after")
    def require_confirmation_consistency(self) -> ReviewCommandDraft:
        if self.needs_confirmation and not self.confirmation_reason:
            raise PydanticCustomError(
                "confirmation_reason", "flagged drafts must state the ambiguity"
            )
        if not self.needs_confirmation and self.confirmation_reason is not None:
            raise PydanticCustomError(
                "confirmation_reason", "clear drafts carry no confirmation reason"
            )
        if self.command_kind is None and not self.needs_confirmation:
            raise PydanticCustomError(
                "command_kind", "unrecognized drafts always need confirmation"
            )
        return self


class AppliedCommand(StrictModel):
    """One applied review command: the audit entry rebuild plans derive from."""

    schema_version: Literal["cockpit-applied-command-v1"] = "cockpit-applied-command-v1"
    command_id: Identifier
    command_kind: ReviewCommandKind
    affected_domain: AffectedDomain
    event_id: Sha256 | None = None
    base_plan_version: str | None = None
    result_plan_version: str | None = None
    deferred: bool = False
    reason: str | None = None
    target_seconds: Seconds | None = None
    seconds_delta: Seconds | None = None


class RebuildPlan(StrictModel):
    """Lineage-scoped stage set for one applied command (unrelated excluded)."""

    schema_version: Literal["cockpit-rebuild-plan-v1"] = "cockpit-rebuild-plan-v1"
    command_id: Identifier
    command_kind: ReviewCommandKind
    affected_domain: AffectedDomain
    stages: StageSequence
    excluded_stages: StageSequence

    @model_validator(mode="after")
    def require_disjoint_stages(self) -> RebuildPlan:
        if set(self.stages) & set(self.excluded_stages):
            raise PydanticCustomError(
                "stage_overlap", "rebuild stages and excluded stages must be disjoint"
            )
        return self


@dataclass(frozen=True, slots=True)
class ReviewStoreLocation:
    """Paths of one episode's review_command store (sealed log + plan dir)."""

    log_path: Path
    plan_dir: Path


class _KindRule(NamedTuple):
    kind: ReviewCommandKind
    pattern: re.Pattern[str]
    position_dependent: bool


# Ordered kind table (first match wins): specific effects/subtitles before the
# generic section verbs; an explicit delete verb beats the boring sentiment.
_RULE_SPEC: tuple[tuple[ReviewCommandKind, str, bool], ...] = (
    (
        "keep_longer",
        (
            r"\d+(?:\.\d+)?\s*秒[^。、\n]{0,6}(?:残し|長く)"
            r"|keep\s+\d+(?:\.\d+)?\s+(?:more\s+)?seconds?"
            r"|\d+(?:\.\d+)?\s+more\s+seconds?|keep\s+\d+(?:\.\d+)?\s+seconds?\s+more"
        ),
        True,
    ),
    (
        "quiet_longer",
        (r"(?:静か|無音)[^。、\n]{0,12}(?:長く|残し)|quiet[^.\n]{0,30}longer"
        r"|leave\s+the\s+quiet"),
        True,
    ),
    ("subtitle_shorter", r"字幕[^。、\n]{0,10}短く|subtitles?\s+shorter", False),
    (
        "remove_effect",
        (r"ズーム[^。、\n]{0,8}(?:やめ|止め|なし|削除|解除)|remove\s+the\s+zoom"
        r"|zoom\s+effect\s+(?:off|remove)|no\s+zoom|without\s+zoom"),
        False,
    ),
    (
        "lower_bgm",
        (r"BGM[^。、\n]{0,10}(?:小さく|下げ|小さめ|音量下)|lower\s+the\s+bgm"
        r"|bgm[^.\n]{0,20}(?:lower|quieter|softer|down)"
        r"|turn\s+(?:down|lower)\s+(?:the\s+)?bgm"),
        True,
    ),
    (
        "match_color",
        (r"色[^。、\n]{0,8}(?:合わせ|揃え)|色味|カラーマッチ"
        r"|match[^.\n]{0,30}color|color\s+match"),
        False,
    ),
    (
        "channel_lower_third",
        (r"テロップ[^。、\n]{0,20}チャンネル|チャンネル[^。、\n]{0,20}テロップ"
        r"|lower.?third[^.\n]{0,40}channel|channel[^.\n]{0,40}lower.?third"),
        False,
    ),
    (
        "use_other_take",
        (r"(?:別|違う|他)の?(?:テイク|テーク)|もう一つの(?:テイク|テーク)"
        r"|(?:other|different|another)\s+take"),
        True,
    ),
    (
        "insert_broll",
        (r"Bロール[^。、\n]{0,10}(?:追加|入れ|足し)|もっとBロール"
        r"|b-?roll[^.\n]{0,20}(?:insert|more|add)"
        r"|(?:insert|add)\s+(?:more\s+)?b-?roll|more\s+b-?roll"),
        True,
    ),
    (
        "remove_section",
        (r"(?:この|その|あの)[^。、\n]{0,8}(?:削除|カット|消し|切り取)"
        r"|(?:区間|部分|シーン|場面|箇所)を?(?:削除|カット)"
        r"|(?:ところ|箇所|位置|時点)を?(?:削除|カット|消し)"
        r"|(?:remove|cut|delete|drop)\s+(?:this|that|the)\s+"
        r"(?:section|part|shot|scene|segment)"),
        True,
    ),
    ("mark_boring", r"退屈|つまらな|面白くない|眠たくな|\bboring\b|\bdull\b", True),
    (
        "episode_only",
        (r"このエピソードだけ|このエピソードのみ|今回だけ|今回のみ"
        r"|only\s+for\s+this\s+episode|this\s+episode\s+only|just\s+this\s+episode"),
        False,
    ),
)
_KIND_RULES = tuple(
    _KindRule(kind, re.compile(pattern, re.IGNORECASE), positional)
    for kind, pattern, positional in _RULE_SPEC
)
_POSITION_DEPENDENT = frozenset(rule.kind for rule in _KIND_RULES if rule.position_dependent)
_POSITION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(\d{1,2}):(\d{2})(?:\.(\d+))?",
        r"(\d+)分(\d+)秒(?:\.(\d+))?",
        r"(?:at|@)\s*(\d+(?:\.\d+)?)\s*s?\b",
        r"(\d+(?:\.\d+)?)秒(?:のところ|地点|あたり|から|以降|の位置|で)",
    )
)
_DELTA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(\d+(?:\.\d+)?)\s*秒(?!(?:のところ|地点|あたり|から|以降|の位置|で))",
        r"(\d+(?:\.\d+)?)\s*(?:more\s+)?seconds?",
    )
)
_APPLICABLE_KINDS: Mapping[ReviewCommandKind, SpanOperation] = {
    "remove_section": "remove_segment",
    "keep_longer": "adjust_source_span",
    "quiet_longer": "adjust_source_span",
}


def _first_group_float(patterns: tuple[re.Pattern[str], ...], text: str) -> float | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None


def _resolve_target(text: str, at_seconds: float | None) -> float | None:
    """Explicit in-message time references win over the player position."""

    for pattern in _POSITION_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groups()
        if groups[1:2]:
            total = int(groups[0]) * 60 + int(groups[1])
            fraction = groups[2] if groups[2:3] else None
            if fraction is not None:
                return total + float(f"0.{fraction}")
            return float(total)
        return float(groups[0])
    return at_seconds


def _command_id(
    kind: ReviewCommandKind | None, target: float | None, delta: float | None, text: str
) -> str:
    seed = f"{kind}|{target}|{delta}|{text}"
    return "rcmd-" + hashlib.sha256(seed.encode()).hexdigest()[:12]


def interpret_command(text: str, context: ReviewChatContext) -> ReviewCommandDraft:
    """Deterministically interpret one review message (Japanese-first, no LLM)."""

    kind: ReviewCommandKind | None = next(
        (rule.kind for rule in _KIND_RULES if rule.pattern.search(text)), None
    )
    target = _resolve_target(text, context.at_seconds)
    delta = _first_group_float(_DELTA_PATTERNS, text) if kind == "keep_longer" else None
    scope = "channel" if kind == "channel_lower_third" else "episode"
    reason: str | None = None
    if kind is None:
        reason = "no known command kind matched the message; restate the correction"
    elif kind in _POSITION_DEPENDENT and target is None:
        reason = (
            "no target timestamp: set the player position or name an explicit "
            "time in the message"
        )
    return ReviewCommandDraft(
        command_id=_command_id(kind, target, delta, text),
        command_kind=kind,
        text=text,
        target_seconds=target,
        seconds_delta=delta,
        scope=scope,
        needs_confirmation=reason is not None,
        confirmation_reason=reason,
    )


def _covering_video_items(plan: EditPlan0C, frame: int) -> tuple[EditPlanItem0C, ...]:
    covering = [
        item
        for item in plan.plan.items
        if item.kind == "video" and item.span.start_frame <= frame < item.span.end_frame
    ]
    return tuple(sorted(covering, key=lambda item: (item.track_index, item.item_id)))


def _require_covering(head: HeadState, frame: int) -> tuple[EditPlanItem0C, ...]:
    items = _covering_video_items(head.plan, frame)
    if not items:
        raise ReviewChatError(
            "target-not-in-plan",
            f"frame {frame} covers no video item of plan v{head.version}",
        )
    return items


def _translate_remove(
    draft: ReviewCommandDraft, head: HeadState, frame: int
) -> RemoveSegmentProposal0C:
    targets = _require_covering(head, frame)
    return RemoveSegmentProposal0C(
        proposal_id=f"prop-{draft.command_id[5:]}",
        command_kind="remove_segment",
        base_plan_version=f"v{head.version}",
        actor_intent="operator",
        sequence=0,
        confidence=Confidence0C(num=1, den=1),
        ambiguity=ProposalAmbiguity0C(status="clear"),
        evidence=(draft.text,),
        candidate_targets=tuple(
            CandidateTarget0C(item_id=item.item_id, evidence=draft.text) for item in targets
        ),
    )


def _translate_adjust(
    draft: ReviewCommandDraft, head: HeadState, frame: int, delta: float
) -> AdjustSourceSpanProposal0C:
    item = _require_covering(head, frame)[0]  # deterministic: lowest track, then id
    delta_frames = int(delta * head.plan.frame_rate.as_fraction + 0.5)
    total = head.plan.plan.edit_source.total_frames
    return AdjustSourceSpanProposal0C(
        proposal_id=f"prop-{draft.command_id[5:]}",
        command_kind="adjust_source_span",
        base_plan_version=f"v{head.version}",
        actor_intent="operator",
        sequence=0,
        confidence=Confidence0C(num=1, den=1),
        ambiguity=ProposalAmbiguity0C(status="clear"),
        evidence=(draft.text,),
        target=ItemIdSelector0C(kind="item_id", item_id=item.item_id),
        new_span=SpanBounds0C(
            start_frame=item.span.start_frame,
            end_frame=min(item.span.end_frame + delta_frames, total),
        ),
    )


def apply_command(draft: ReviewCommandDraft, *, store: ReviewStoreLocation) -> AppliedCommand:
    """Apply an approved draft: 0C event+version where translatable, else intent."""

    if draft.needs_confirmation or draft.command_kind is None:
        raise ReviewChatError("draft-not-confirmed", "only confirmed drafts may be applied")
    kind = draft.command_kind
    operation = _APPLICABLE_KINDS.get(kind)
    if operation is None:
        return AppliedCommand(
            command_id=draft.command_id,
            command_kind=kind,
            affected_domain=COMMAND_DOMAIN[kind],
            target_seconds=draft.target_seconds,
            seconds_delta=draft.seconds_delta,
        )
    if draft.target_seconds is None:
        raise ReviewChatError("target-required", f"{kind} needs a target timestamp")
    head = load_head(store.log_path, store.plan_dir)
    frame = int(draft.target_seconds * head.plan.frame_rate.as_fraction + 0.5)
    if operation == "adjust_source_span":
        if draft.seconds_delta is None:
            raise ReviewChatError("delta-required", f"{kind} needs an explicit seconds amount")
        proposal = _translate_adjust(draft, head, frame, draft.seconds_delta)
    else:
        proposal = _translate_remove(draft, head, frame)
    decision = OperatorDecision0C(
        decision_id=f"dec-{draft.command_id[5:]}", actor_intent="operator", note=draft.text
    )
    outcome: CommitOutcome = commit_command(proposal, decision, store.log_path, store.plan_dir)
    return AppliedCommand(
        command_id=draft.command_id,
        command_kind=kind,
        affected_domain=COMMAND_DOMAIN[kind],
        event_id=outcome.event_id,
        base_plan_version=f"v{head.version}",
        result_plan_version=None if outcome.deferred else f"v{outcome.version}",
        deferred=outcome.deferred,
        reason=outcome.reason,
        target_seconds=draft.target_seconds,
        seconds_delta=draft.seconds_delta,
    )


def plan_rebuild(affected: AppliedCommand, lineage: StageLineage) -> RebuildPlan:
    """Map an applied command to the minimal dependent stage set (partial rebuild)."""

    if affected.deferred:
        raise ReviewChatError(
            "command-deferred",
            f"command {affected.command_id} was deferred ({affected.reason}); "
            "a deferred command rebuilds nothing",
        )
    domain_stages = lineage.get(affected.affected_domain)
    if domain_stages is None:
        raise ReviewChatError(
            "unknown-domain", f"lineage does not cover domain {affected.affected_domain}"
        )
    unknown = domain_stages.difference(PIPELINE_STAGES)
    if unknown:
        raise ReviewChatError(
            "unknown-stage", f"lineage stages outside the pipeline: {sorted(unknown)}"
        )
    return RebuildPlan(
        command_id=affected.command_id,
        command_kind=affected.command_kind,
        affected_domain=affected.affected_domain,
        stages=tuple(stage for stage in PIPELINE_STAGES if stage in domain_stages),
        excluded_stages=tuple(stage for stage in PIPELINE_STAGES if stage not in domain_stages),
    )


def record_applied_command(episode_dir: Path, applied: AppliedCommand) -> None:
    """Append one applied command to the episode's additive audit log."""

    log_path = episode_dir / APPLIED_COMMANDS_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write(canonical_model_bytes(applied) + b"\n")


def load_applied_command(episode_dir: Path, command_id: str) -> AppliedCommand:
    """Read back an applied command; the first (applied) entry wins over replays."""

    log_path = episode_dir / APPLIED_COMMANDS_NAME
    try:
        lines = log_path.read_bytes().splitlines()
    except OSError as error:
        raise CockpitNotFoundError(
            "applied-command-not-found", f"no applied command {command_id}"
        ) from error
    for line in lines:
        try:
            applied = AppliedCommand.model_validate_json(line)
        except ValidationError as error:
            raise ReviewChatError(
                "applied-command-log-corrupt", f"unparsable line in {log_path}"
            ) from error
        if applied.command_id == command_id:
            return applied
    raise CockpitNotFoundError(
        "applied-command-not-found", f"no applied command {command_id}"
    )


__all__ = [
    "APPLIED_COMMANDS_NAME",
    "COMMAND_DOMAIN",
    "DEFAULT_LINEAGE",
    "PIPELINE_STAGES",
    "AppliedCommand",
    "RebuildPlan",
    "ReviewChatContext",
    "ReviewChatError",
    "ReviewCommandDraft",
    "ReviewStoreLocation",
    "apply_command",
    "interpret_command",
    "load_applied_command",
    "plan_rebuild",
    "record_applied_command",
]
