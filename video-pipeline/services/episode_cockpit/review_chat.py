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

# allow: SIZE_OK — ~640 pure LOC under a plan-pinned single-file commit scope
# (task 47: review_chat.py only; the UX redesign 工程1 review-fix delta added
# the feelings-display docstrings + the investigated-bridge authority note;
# 工程2 adds the reaction classifier + prior-proposal context + the honest
# no-proposal draft builder, and the rework adds the shared pre-commit path
# (_prepare_commit/validate_draft_applicable) + the proposal-kind context,
# and rework #1 adds the gated FrameMaterial context field, and rework
# round 2 adds the _DELTA_KINDS bridge + the simulation head override on
# _prepare_commit (P1-1) — same parser/store file by design);
# ~120 LOC are the pure command-pattern data table plus immutable
# store/lineage tables. Split parser-vs-apply/plan into two modules at
# task-51 wiring when models.py unlocks.

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
from services.episode_cockpit.models import (  # noqa: TC001 (pydantic runtime)
    FrameMaterial,
    NonEmpty,
    ProposalKind,
    ReviewReactionKind,
    Seconds,
    SequenceNumber,
)
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
    "split_display",
    "duration_shorten",
    "text_summary_ack",
    "line_wrap",
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
    "split_display": "presentation",
    "duration_shorten": "presentation",
    "text_summary_ack": "presentation",
    "line_wrap": "presentation",
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

# Subtitle 4-choice confirmation (v4 P1-E): the ambiguous 「字幕を短く」
# (subtitle_shorter) NEVER auto-maps to wrap-width narrowing. The draft is
# flagged needs_confirmation with this plain-Japanese question; the
# operator answers with an explicit message that parses to one of the four
# distinct kinds below (split_display / duration_shorten /
# text_summary_ack / line_wrap). Shape mirrors the existing
# needs_confirmation flow — the UI/apply contract is unchanged.
SUBTITLE_CHOICE_SPLIT = "言葉は変えず、一度に出す文字を少なく分ける"
SUBTITLE_CHOICE_DURATION = "表示している時間を短くする"
SUBTITLE_CHOICE_SUMMARY = (
    "話した内容を要約して文章自体を短くする"
    "（発話どおりではなくなる——明示了承が必要）"  # noqa: RUF001 (fullwidth parens are the required JA wording)
)
SUBTITLE_CHOICE_WRAP = "一行の幅だけ狭くして折り返す"
SUBTITLE_AMBIGUOUS_QUESTION = (
    "「字幕を短く」には4つの意味があります。番号で教えてください:\n"
    f"1) {SUBTITLE_CHOICE_SPLIT}\n"
    f"2) {SUBTITLE_CHOICE_DURATION}\n"
    f"3) {SUBTITLE_CHOICE_SUMMARY}\n"
    f"4) {SUBTITLE_CHOICE_WRAP}"
)
SUBTITLE_SUMMARY_ACK_REQUEST = (
    "字幕の要約は発話どおりではなくなります。よければ「要約してよい」など"
    "明示の了承を添えて送ってください"
)
# Contract for the three 4-choice kinds with no review-plane knob
# (split_display / duration_shorten / text_summary_ack): all four options
# stay visible as disambiguation choices; selecting a no-knob kind raises
# the refusal below — no AppliedCommand is journaled, no rebuild scheduled.
# Only line_wrap maps to the real wrap-width override; text_summary_ack
# has no summary body to write, so an explicit ack alone changes nothing.
SUBTITLE_NO_KNOB_REFUSAL_DETAIL = (
    "現在の仕組みではまだ対応していません。"
    "別の選択肢を選ぶか、相談へ戻ってください。"
)
_SUBTITLE_NO_KNOB_REFUSAL_KINDS: frozenset[ReviewCommandKind] = frozenset(
    {"split_display", "duration_shorten", "text_summary_ack"}
)


class ReviewChatError(CockpitUnprocessableError):
    """Typed review-chat parse/apply failure (structured 422 at the boundary)."""


class ReviewCommandDraft(StrictModel):
    """Deterministic interpretation preview of one natural-language message.

    ``hypothesis`` / ``investigated`` are DISPLAY-ONLY fields carried by the
    feelings route (cause investigation): they never enter the command_id
    hash and never relax confirmation — an investigated draft still needs
    the operator's explicit apply to commit. ``investigated`` is a
    materials-gathered flag (transcript/scene text only; 工程1 never checks
    the actual video/audio) — NEVER a cause-identified claim: with no
    hypothesis the honest display is 「周辺の字幕と場面情報を確認しましたが、
    原因はまだ特定できていません」, never 「原因を調査しました」.
    """

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
    hypothesis: str | None = None
    investigated: bool = False
    explicit_ack: bool = False

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


class PriorProposalContext(StrictModel):
    """工程2: the earlier proposal set a reaction message responds to.

    DATA for the LLM (never instructions): the drafts it may adjust plus
    the display state of that earlier investigation. Rides
    ``ReviewChatContext`` so the transport-agnostic LLM call signature is
    unchanged and old fakes keep working.
    """

    set_sequence: SequenceNumber
    drafts: Annotated[tuple[ReviewCommandDraft, ...], BeforeValidator(tuple)] = ()
    investigation_state: str | None = None


class ReviewChatContext(StrictModel):
    """Where the reviewer was when the message was sent.

    工程2 (additive): ``reaction_kind`` / ``prior_set`` carry the proposal
    conversation a reaction message continues — DATA for the LLM data
    block (``review_interpreter._request_parts``), never instructions;
    the deterministic interpreter ignores them.

    工程2 rework: ``proposal_kind`` selects the LLM proposal mode the
    route runs in — ``command-bundle`` (explicit fixes, enumerate ALL) or
    ``alternatives`` (mutually exclusive choices, max 2). It shapes the
    interpreter instructions and the deterministic per-mode cap; the
    SAVED set records the same kind (``review_proposals``).

    工程2 rework #1 (additive, PriorProposalContext precedent):
    ``frame_materials`` carries the checked video STILLS as DATA for the
    LLM (gated on by ``review_frame_materials.enabled``). A still is one
    frame — never an audio or whole-video verification.
    """

    at_seconds: Seconds | None = None
    reaction_kind: ReviewReactionKind | None = None
    prior_set: PriorProposalContext | None = None
    proposal_kind: ProposalKind = "command-bundle"
    frame_materials: tuple[FrameMaterial, ...] = ()


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
    explicit_ack: bool = False


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
    (
        "split_display",
        (r"字幕[^。、\n]{0,10}(?:分けて表示|分けて|分割|小分け)"
        r"|split\s+(?:the\s+)?subtitles?"),
        False,
    ),
    (
        "duration_shorten",
        (r"字幕[^。、\n]{0,10}(?:表示時間|表示)[^。、\n]{0,15}(?:短く|縮め)"
        r"|(?:shorten|reduce)\s+(?:the\s+)?subtitle\s+(?:display|duration|time)"
        r"|subtitle\s+(?:display|duration)\s+short"),
        False,
    ),
    (
        "text_summary_ack",
        (r"字幕[^。、\n]{0,10}(?:要約|まとめ)"
        r"|summariz\w*\s+(?:the\s+)?subtitles?|subtitles?\s+summar"),
        False,
    ),
    (
        "line_wrap",
        (r"字幕[^。、\n]{0,10}(?:折り返|改行|幅を?狭く|一行)"
        r"|(?:wrap|narrow)\s+(?:the\s+)?subtitles?|subtitles?\s+wrap"),
        False,
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
_SUMMARY_ACK_PATTERN = re.compile(
    r"了承|承知|承認|かまわない|構わない|大丈夫|いいよ|OK|オッケー|よい|良い",
    re.IGNORECASE,
)
# Feelings/goal vocabulary (UX redesign 工程1, JA-first): a match routes the
# message through cause investigation instead of positional auto-confirm.
_FEELINGS_PATTERN = re.compile(
    r"退屈|つまらな|(?:面白|おもしろ)くない|眠たくな|素人っぽい|映画っぽく"
    r"|\bboring\b|\bdull\b",
    re.IGNORECASE,
)
_FEELINGS_KINDS = frozenset({"mark_boring"})
_FEELINGS_REASON = (
    "this expresses a feeling, not a concrete change; the cause is "
    "investigated before any change is proposed"
)
# Reaction vocabulary (UX redesign 工程2, brief §3.3/§6.2): a message that
# reacts to the PREVIEWED proposal set instead of naming a new correction.
# Classification precedence: a direct _RULE_SPEC command always wins; then
# the explicit both-different rejection; then the pairwise choice; then the
# continuation/adjustment vocabulary. Choice records the CHOICE FACT only —
# a reason is never asked for and never recorded (U04).
_BOTH_DIFFERENT_PATTERN = re.compile(
    r"両方[ととも]?違う|どちらも違う|どっちも違う|どれも違う"
    r"|両方[ととも]?だめ|どちらもだめ|どっちもだめ"
)
_CHOICE_A_PATTERN = re.compile(
    r"(?:^|[^A-Za-z0-9])[AaＡ]\s*(?:が|で|を|に|だ|です)|1番目|一番目|最初の方",  # noqa: RUF001 (fullwidth letter is intentional JA input)
    re.IGNORECASE,
)
_CHOICE_B_PATTERN = re.compile(
    r"(?:^|[^A-Za-z0-9])[BbＢ]\s*(?:が|で|を|に|だ|です)|2番目|二番目|2つ目|二つ目|後の方",  # noqa: RUF001 (fullwidth letter is intentional JA input)
    re.IGNORECASE,
)
_CONTINUATION_PATTERN = re.compile(
    r"前より|もっと|さっきの|さっき|今の|前回|今回は|落ち着|せわしな"
    r"|速すぎ|遅すぎ|早すぎ|きつ|目に優し"
)
_REJECTION_FALLBACK_REASON = (
    "両方とも違うとの反応を記録しました。原因の再調査が必要です"
)
_CONTINUATION_FALLBACK_REASON = (
    "前の提案を踏まえた調整の依頼として記録しました。原因の特定には"
    "追加の確認が必要です"
)
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
# Span-length kinds: the apply side translates them with an explicit
# seconds amount (_prepare_commit: delta-required otherwise), so the
# parsers must carry it for both — matching _APPLICABLE_KINDS.
_DELTA_KINDS: frozenset[ReviewCommandKind] = frozenset({"keep_longer", "quiet_longer"})


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


def _is_feelings_interpretation(kind: ReviewCommandKind | None, text: str) -> bool:
    """Feelings interpretation: the sentiment IS the message (its kind is the
    boring marker or nothing concrete matched) — explicit command verbs win."""

    if kind is not None:
        return kind in _FEELINGS_KINDS
    return _FEELINGS_PATTERN.search(text) is not None


def classify_reaction(text: str) -> ReviewReactionKind | None:
    """工程2 U02-U05: classify a message that reacts to the previewed
    proposal set (closed Literal set). A direct _RULE_SPEC command ALWAYS
    wins (「退屈なところを削除して」 is a command; 「今回だけ」 beats the
    continuation 「今回は」); without one, an explicit both-different
    rejection beats a pairwise choice, which beats continuation vocabulary.
    """

    if any(rule.pattern.search(text) for rule in _KIND_RULES):
        return None
    if _BOTH_DIFFERENT_PATTERN.search(text):
        return "both-different"
    if _CHOICE_B_PATTERN.search(text):
        return "choice-b"
    if _CHOICE_A_PATTERN.search(text):
        return "choice-a"
    if _CONTINUATION_PATTERN.search(text):
        return "continuation"
    return None


def reaction_no_proposal_draft(text: str, reason: str) -> ReviewCommandDraft:
    """Honest flagged draft when a reaction must not (or cannot) produce a
    new proposal: the reaction is recorded, NO new proposal is fabricated,
    and the flag states what is missing (re-investigation)."""

    return ReviewCommandDraft(
        command_id=_command_id(None, None, None, text),
        command_kind=None,
        text=text,
        needs_confirmation=True,
        confirmation_reason=reason,
    )


def with_reaction_fallback_reason(
    drafts: list[ReviewCommandDraft], reason: str
) -> list[ReviewCommandDraft]:
    """Replace the generic unrecognized-input flag on kind-None drafts with
    the reaction-specific honest reason (LLM-less fallback path)."""

    return [
        draft
        if draft.command_kind is not None
        else draft.model_copy(update={"confirmation_reason": reason})
        for draft in drafts
    ]


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
    delta = _first_group_float(_DELTA_PATTERNS, text) if kind in _DELTA_KINDS else None
    scope = "channel" if kind == "channel_lower_third" else "episode"
    explicit_ack = kind == "text_summary_ack" and _SUMMARY_ACK_PATTERN.search(text) is not None
    reason: str | None = None
    if _is_feelings_interpretation(kind, text):
        # Feelings NEVER auto-confirm from position alone (brief rule 2/3):
        # the cause-investigation route decides the change.
        reason = _FEELINGS_REASON
    elif kind == "subtitle_shorter":
        reason = SUBTITLE_AMBIGUOUS_QUESTION
    elif kind == "text_summary_ack" and not explicit_ack:
        reason = SUBTITLE_SUMMARY_ACK_REQUEST
    elif kind is None:
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
        explicit_ack=explicit_ack,
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


def _prepare_commit(
    draft: ReviewCommandDraft,
    *,
    store: ReviewStoreLocation,
    head: HeadState | None = None,
) -> (
    tuple[
        HeadState,
        RemoveSegmentProposal0C | AdjustSourceSpanProposal0C,
        OperatorDecision0C,
    ]
    | None
):
    """Shared pre-commit path of ``apply_command``/``validate_draft_applicable``:
    every deterministic check plus proposal translation, NO writes. ``None``
    means an intent-only command (nothing to translate or commit). ``head``
    overrides the store head — apply_drafts passes the PROVISIONAL head of
    its sequential simulation (P1-1: draft i+1 is validated against the
    plan AFTER drafts 1..i applied)."""

    if draft.command_kind is None:
        raise ReviewChatError("draft-not-confirmed", "only confirmed drafts may be applied")
    if draft.needs_confirmation and not draft.investigated:
        raise ReviewChatError("draft-not-confirmed", "only confirmed drafts may be applied")
    kind = draft.command_kind
    if kind in _SUBTITLE_NO_KNOB_REFUSAL_KINDS:
        raise ReviewChatError(
            "subtitle-choice-not-implemented", SUBTITLE_NO_KNOB_REFUSAL_DETAIL
        )
    operation = _APPLICABLE_KINDS.get(kind)
    if operation is None:
        return None
    if draft.target_seconds is None:
        raise ReviewChatError("target-required", f"{kind} needs a target timestamp")
    if head is None:
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
    return head, proposal, decision


def validate_draft_applicable(draft: ReviewCommandDraft, *, store: ReviewStoreLocation) -> None:
    """Run every check ``apply_command`` performs BEFORE its commit; the
    same typed errors surface early (工程2 atomicity: a batch is fully
    validated against the current plan state before anything commits)."""

    _prepare_commit(draft, store=store)


def apply_command(draft: ReviewCommandDraft, *, store: ReviewStoreLocation) -> AppliedCommand:
    """Apply an approved draft: 0C event+version where translatable, else intent.

    Confirmation is ``needs_confirmation=False``, OR an investigated
    (feelings-route) draft: there the operator's explicit apply call of the
    echoed draft IS the confirmation of the investigated proposal. The
    bridge is safe because the apply route only hands over drafts that
    field-for-field equal a SERVER-SAVED proposal set
    (``review_proposals.resolve_authoritative_drafts``) — a fabricated
    ``investigated`` flag can never reach here (brief §5.3).
    """

    prepared = _prepare_commit(draft, store=store)
    if draft.command_kind is None:  # unreachable: _prepare_commit rejects None kinds
        raise ReviewChatError("draft-not-confirmed", "only confirmed drafts may be applied")
    kind = draft.command_kind
    if prepared is None:
        return AppliedCommand(
            command_id=draft.command_id,
            command_kind=kind,
            affected_domain=COMMAND_DOMAIN[kind],
            target_seconds=draft.target_seconds,
            seconds_delta=draft.seconds_delta,
            explicit_ack=draft.explicit_ack,
        )
    head, proposal, decision = prepared
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
    "SUBTITLE_AMBIGUOUS_QUESTION",
    "SUBTITLE_CHOICE_DURATION",
    "SUBTITLE_CHOICE_SPLIT",
    "SUBTITLE_CHOICE_SUMMARY",
    "SUBTITLE_CHOICE_WRAP",
    "SUBTITLE_NO_KNOB_REFUSAL_DETAIL",
    "SUBTITLE_SUMMARY_ACK_REQUEST",
    "AppliedCommand",
    "PriorProposalContext",
    "RebuildPlan",
    "ReviewChatContext",
    "ReviewChatError",
    "ReviewCommandDraft",
    "ReviewStoreLocation",
    "apply_command",
    "classify_reaction",
    "interpret_command",
    "load_applied_command",
    "plan_rebuild",
    "reaction_no_proposal_draft",
    "record_applied_command",
    "validate_draft_applicable",
    "with_reaction_fallback_reason",
]
