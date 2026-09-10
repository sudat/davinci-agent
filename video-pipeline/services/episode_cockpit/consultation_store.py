"""Consultation journal store — pre-plan consultations (UX phase 2.5 slice-1).

Append-only jsonl journals under ``episodes/<id>/consultation/`` following
the review-store record discipline (``review_proposals.py``): StrictModel
records, canonical bytes, one append-only line per event, and a full
reload that reconstructs state from the journal alone. This is runtime
episode-dir state, NEVER a new authoritative artifact family.

Budget accounting is cumulative for the episode's consultation phase and
is NEVER reset by a retry/regeneration: every generation appends one
budget-event line and ``budget_used`` sums the WHOLE journal. Limits come
from ``config/consultation.json`` with the defaults (LLM calls 6 /
intervals 3 / wall seconds 600) as fallback; ``cost_display`` stays
"unmeasured" (slice 1 measures no cost).
"""

# allow: SIZE_OK — one consultation-journal concern per file (the record
# family + its append/reload + the budget accounting + the pinned view
# builder), the review_proposals.py shape; splitting the models from their
# loaders would fork the same cohesive concern the task pinned to THIS
# module, so the ceiling is declared instead.


from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple

from pydantic import BeforeValidator, Field, ValidationError

from services.contracts.primitives import StrictModel
from services.editorial.models import (
    AdoptedPolicyScopeV1,
    AdoptedPolicySummaryV1,
    PresentationCondition,
)
from services.episode_cockpit.consultation_authorization import (
    FullAuthorizationBinding,
    append_full_authorization_once,
    full_authorization_content_key,
    full_render_authorized,
)
from services.episode_cockpit.consultation_selection_budget import (
    DIRECTOR_WALL_ALLOWANCE_SECONDS,
    BudgetScope,
    selection_budget_used,
    selection_budget_used_in_scope,
)
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import RebuildRequestEntry
from services.episode_cockpit.policy_settings import PolicySettingEntryV1
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.job_runner.state_models import JobSnapshot, StageRunRow

# allow: SIZE_OK — one storage concern per file (the five journal record
# models, the append/reload journal primitives, and the cumulative budget
# accounting over those SAME journals); the record classes and their
# journals stay together exactly like ``review_proposals.py``.

CONSULTATION_DIR_NAME = "consultation"
CONSULTATIONS_NAME = "consultations.jsonl"
PROPOSALS_NAME = "proposals.jsonl"
JUDGMENTS_NAME = "judgments.jsonl"
BUDGET_NAME = "budget.jsonl"
OUTCOMES_NAME = "policy-outcomes.jsonl"
REBUILD_LOG_NAME = "rebuild-requests.jsonl"
# 工程4 (optional generated storyboard, default OFF): consultation-journal
# runtime records — NEVER a new authoritative artifact family. Panels ride
# proposal-set entries; generation events record the U47 model verification
# ({model_selected, verified}) and every refusal note.
PANELS_NAME = "panels.jsonl"
GENERATION_NAME = "generation.jsonl"
PANELS_DIR_NAME = "panels"

CONFIG_RELATIVE = Path("config") / "consultation.json"
_CONFIG_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LLM_CALLS_LIMIT = 6
DEFAULT_INTERVALS_LIMIT = 3
DEFAULT_WALL_SECONDS_LIMIT = 600.0
DEFAULT_PREVIEW_SAMPLE_SECONDS_LIMIT = 30.0
# Episode-cumulative image-call cap (image calls fold into the SAME
# cumulative budget, scope-tagged via the event kind, never reset).
DEFAULT_GENERATION_IMAGES_LIMIT = 6

# Per-proposal panel cap (U32: max 3 panels per proposal, 1-2 proposals).
MAX_PANELS_PER_PROPOSAL = 3
MAX_GENERATION_PROPOSALS = 2

# Canonical scope order for reservation pins and unaddressed derivation.
POLICY_SCOPE_ORDER: tuple[str, ...] = ("composition", "appearance", "audio")

# The only structural checks a successful v1 derivation may record (the
# solve/generate/compile_ir/project_plan + review-store round-trip steps).
# Never relabeled as semantic-compliance evidence.
STRUCTURAL_REALIZED_CHECKS: tuple[str, ...] = (
    "planner_feasibility",
    "edit_plan_generation",
    "production_compile",
    "review_projection",
    "review_store_round_trip",
)

CONNECTED_POLICY_FIELDS: tuple[str, ...] = (
    "decision",
    "scope",
    "audience_message",
    "structure",
    "duration_estimate",
    "candidate_scenes",
    "subtitle_policy",
    "audio_policy",
    "tempo_policy",
    "reference_mapping",
    "unused_reasons",
    "unconfirmed",
    "note",
    "presentation_condition",
)

SCOPE_UNADDRESSED_JA: dict[str, str] = {
    "composition": "構成・候補場面・想定尺・参考対応・テンポが方針の意味どおりか",
    "appearance": "字幕と見た目が実映像で方針どおりか",
    "audio": "BGM・音量・音付きテンポが方針どおりか",
}

TRIAL_VIEW_UNCONFIRMED_JA = "試し編集を本人が見て方針どおりか"


def unaddressed_for_scope(policy: AdoptedPolicyV1) -> tuple[str, ...]:
    flags = (
        ("composition", policy.scope.composition),
        ("appearance", policy.scope.appearance),
        ("audio", policy.scope.audio),
    )
    return tuple(SCOPE_UNADDRESSED_JA[name] for name, flag in flags if flag)


def unconfirmed_for_policy(policy: AdoptedPolicyV1) -> tuple[str, ...]:
    merged = (*policy.unconfirmed, TRIAL_VIEW_UNCONFIRMED_JA)
    return tuple(dict.fromkeys(merged))

type ConsultationDecision = Literal[
    "adopt", "revise", "reject", "both_wrong", "delegate", "full_authorized"
]

type PanelRole = Literal["a", "b", "real_frame"]

type PanelStatus = Literal["requested", "generated", "failed"]

type GenerationDecision = Literal["granted", "refused", "failed"]

type BudgetEventKind = Literal["text", "image"]

# Strict models never coerce list→tuple; pin the review_proposals BeforeValidator convention.
type StringSequence = Annotated[tuple[str, ...], BeforeValidator(tuple)]


class ConsultationScope(StrictModel):
    """Partial-adoption record: 構成だけ採用/見た目未確認 is expressible.

    Adoption here is NOT plan approval, NOT publish approval, and NOT a
    permanent style save — the three booleans record WHICH aspects the
    operator actually adopted, everything unchecked stays unconfirmed.
    """

    composition: bool = False
    appearance: bool = False
    audio: bool = False


class ConsultationProposalDetails(StrictModel):
    """The fixed ten-field proposal body (pinned consultation contract)."""

    audience_message: str
    structure: str
    duration_estimate: str
    candidate_scenes: StringSequence = ()
    subtitle_policy: str
    audio_policy: str
    tempo_policy: str
    reference_mapping: str
    unused_reasons: str
    unconfirmed: StringSequence = ()
    presentation_condition: PresentationCondition = Field(
        default="normal",
        description=(
            "条件付き提示の分類: normal=通常表示、"
            "location_change_only=場所変更時のみ大テロップ、"
            "local_exception=局所的な例外。字幕の方針の条件部分を分類すること。"
        ),
    )


class ConsultationProposalV1(StrictModel):
    """One proposal: ``proposal_id`` is server-assigned (prop-1, prop-2)."""

    proposal_id: str
    title: str
    summary: str
    details: ConsultationProposalDetails


class ConsultationRecordV1(StrictModel):
    """One consultation: the operator message a proposal set answers."""

    schema_version: Literal["cockpit-consultation-v1"] = "cockpit-consultation-v1"
    consultation_id: str
    created_at: str
    message: str


class ConsultationProposalSetV1(StrictModel):
    """The proposal set currently on the table for one consultation.

    Regeneration appends a NEW set (append-only); readers take the LATEST
    set per consultation. At most TWO proposals — ONE normally, TWO only
    when the direction genuinely splits.
    """

    schema_version: Literal["cockpit-consultation-proposals-v1"] = (
        "cockpit-consultation-proposals-v1"
    )
    consultation_id: str
    created_at: str
    proposals: tuple[ConsultationProposalV1, ...] = Field(min_length=1, max_length=2)


class GenerationPermissionV1(StrictModel):
    """Per-request image-generation permission (工程4, default OFF).

    Absent, or ``granted=False``, means text-only exactly as today — the
    generation seam is never contacted. ``panels_max`` caps panels per
    proposal (1..3); ``images_max`` caps image transport calls for the
    request (>=1). Vague taste (no concrete panel target) never launches:
    the route refuses with 0 calls until the request names its scope.
    """

    granted: bool = False
    panels_max: int = Field(default=1, ge=1, le=3)
    images_max: int = Field(default=1, ge=1)


class PanelCaptionV1(StrictModel):
    """One panel's caption: role subject, scene note, changes, unconfirmed."""

    subject: str = ""
    scene_note: str = ""
    changes: str = ""
    unconfirmed: StringSequence = ()


class ConsultationPanelV1(StrictModel):
    """One storyboard panel riding a proposal-set entry (max 3 per proposal).

    ``role`` ``real_frame`` is an extracted real frame (区分 撮影素材, no
    generation call, ``image_ref`` stays None); ``a``/``b`` are generated
    variants (区分 生成した見た目の案). ``base_created_at`` pins the
    proposal set the panel was generated against — a re-request naming an
    older base is a typed stale refusal (U38), never an overwrite.
    ``error_code`` names a failed panel's honest reason; ``model_selected``
    records the verified U47 model for generated panels.
    """

    schema_version: Literal["cockpit-consultation-panel-v1"] = (
        "cockpit-consultation-panel-v1"
    )
    consultation_id: str
    proposal_id: str
    panel_id: str
    base_created_at: str
    role: PanelRole = "a"
    source_frame_ref: str | None = None
    image_ref: str | None = None
    caption: PanelCaptionV1 = Field(default_factory=PanelCaptionV1)
    status: PanelStatus | None = None
    error_code: str | None = None
    model_selected: str | None = None
    created_at: str


class GenerationEventV1(StrictModel):
    """One generation decision per consultation (U47 record + refusal notes).

    ``verified``/``model_selected`` record the actually-selected-model
    confirmation; ``decision`` is granted (panels attempted), refused (0
    image calls: no permission, vague/out-of-scope, unverified model,
    gated transport, exhausted budget), or failed. ``note`` is the honest
    view-facing sentence; ``reason`` the machine-readable cause.
    """

    schema_version: Literal["cockpit-consultation-generation-v1"] = (
        "cockpit-consultation-generation-v1"
    )
    consultation_id: str
    proposal_id: str | None = None
    created_at: str
    model_selected: str | None = None
    verified: bool = False
    decision: GenerationDecision
    reason: str | None = None
    note: str | None = None


class ConsultationJudgmentV1(StrictModel):
    """One append-only judgment on a consultation (or one of its proposals).

    ``proposal_id=None`` judges the consultation as a whole
    (``both_wrong``/``delegate``). Judgments are never rewritten.

    ``operation_id`` is the stable adoption-operation key (W1): a re-send
    of the same adoption carries the same id (or none, in which case the
    server derives it from the content fingerprint) and dedupes against
    the WHOLE journal — never just the latest row. A deliberate
    re-adoption is a NEW operation id and appends a new row. Absent
    (None) on legacy lines.

    The ``auth_*`` binding (``full_authorized`` only) is server-fixed at
    append time from the current review-store head, the adopted policy,
    and the viewed sample's stored manifest — never client-claimed.
    Absent (None) on every other decision and on legacy unbound
    ``full_authorized`` rows, which authorize nothing (fail-closed).
    """

    schema_version: Literal["cockpit-consultation-judgment-v1"] = (
        "cockpit-consultation-judgment-v1"
    )
    judgment_id: str
    consultation_id: str
    proposal_id: str | None = None
    decision: ConsultationDecision
    scope: ConsultationScope
    note: str | None = None
    operation_id: str | None = None
    auth_sample_id: str | None = None
    auth_sample_content_sha256: str | None = None
    auth_base_version: str | None = None
    auth_base_plan_sha256: str | None = None
    auth_policy_sha256: str | None = None
    created_at: str


class AdoptedPolicyV1(StrictModel):
    """The latest adoptable operator decision joined with its proposal.

    Deterministic extraction (no LLM): the LATEST judgment with decision
    adopt|revise and at least one scope flag, joined with its proposal's
    detail fields plus the judgment note. A NEWER reject/both_wrong (or
    an empty scope, or a missing proposal) yields NO policy — the operator
    withdrew, and the planning path must not reuse the older案.
    """

    schema_version: Literal["cockpit-consultation-policy-v1"] = (
        "cockpit-consultation-policy-v1"
    )
    consultation_id: str
    judgment_id: str
    proposal_id: str
    decision: Literal["adopt", "revise"]
    scope: ConsultationScope
    audience_message: str
    structure: str
    duration_estimate: str
    candidate_scenes: StringSequence = ()
    subtitle_policy: str
    audio_policy: str
    tempo_policy: str
    reference_mapping: str
    unused_reasons: str
    unconfirmed: StringSequence = ()
    note: str | None = None
    presentation_condition: PresentationCondition = "normal"


type PolicyOutcomeStatus = Literal["honored", "connected", "failed"]

type DirectorConnection = Literal["confirmed", "not_started", "unknown"]


class ConsultationPolicyOutcomeV1(StrictModel):
    """One selection-rebuild verdict for an adopted policy (append-only).

    ``honored`` is legacy read-only (old writers emitted it; new writers
    emit only ``connected`` or ``failed``). ``connected`` means the pinned
    policy reached the director request and the listed structural checks
    passed — never a semantic-compliance claim. ``failed`` carries the
    honest reason (never a silently dropped policy) and MAY name the
    recorded ``plan_version`` when a sealed policy event proves the
    version exists (orphan-recovery honesty). ``director_connection``
    separates connection from realization: ``confirmed`` (a live response
    to the pinned-policy request arrived), ``not_started`` (stopped
    before any director contact), ``unknown`` (transport attempted but
    delivery unproven). ``unaddressed`` names adopted-scope aspects whose
    realization is unverified; ``unconfirmed`` names still-unconfirmed
    items. ``note`` records what was actually verified (never beyond it).
    """

    schema_version: Literal["cockpit-consultation-policy-outcome-v1"] = (
        "cockpit-consultation-policy-outcome-v1"
    )
    outcome_id: str
    consultation_id: str
    judgment_id: str
    proposal_id: str | None = None
    plan_version: str | None = None
    status: PolicyOutcomeStatus
    reasons: StringSequence = ()
    note: str | None = None
    created_at: str
    reservation_sequence: int | None = None
    run_id: str | None = None
    commit_event_id: str | None = None
    failure_code: str | None = None
    director_connection: DirectorConnection | None = None
    director_request_hash: str | None = None
    policy_prompt_sha256: str | None = None
    connected_fields: StringSequence = ()
    realized_checks: StringSequence = ()
    unaddressed: StringSequence = ()
    unconfirmed: StringSequence = ()
    # W10: True when this outcome names a plan version that is NO LONGER
    # the review-store head (a record-only past result, never "currently
    # applied"). None on legacy lines = the distinction was not recorded.
    superseded_by_head: bool | None = None
    # W9: the deterministic field→setting rows actually applied to the
    # derived plan (empty on reused/failed outcomes: nothing was derived).
    applied_settings: tuple[PolicySettingEntryV1, ...] = ()


class ConsultationBudgetEventV1(StrictModel):
    """One generation's consumption; cumulative state = the journal sum.

    ``model_id`` attributes the spend to one production model (None =
    unattributed, e.g. legacy lines or transports that do not report
    it). ``input_bytes``/``output_bytes`` are measured transport sizes
    (None = unmeasured). ``retry_index`` itemizes internal retries so a
    retried generation settles one line per attempt. ``failure_code``
    marks a failed generation's line — failures still consume budget,
    never silently. ``kind`` scope-tags image consumption (工程4): image
    calls fold into the SAME cumulative counters and NEVER reset the
    ledger; legacy lines read back as ``text``.
    """

    schema_version: Literal["cockpit-consultation-budget-v1"] = (
        "cockpit-consultation-budget-v1"
    )
    consultation_id: str
    llm_calls: int = Field(ge=0)
    intervals: int = Field(ge=0)
    wall_seconds: Annotated[float, BeforeValidator(float)] = Field(ge=0.0)
    kind: BudgetEventKind = "text"
    model_id: str | None = None
    input_bytes: int | None = Field(default=None, ge=0)
    output_bytes: int | None = Field(default=None, ge=0)
    retry_index: int = Field(default=0, ge=0)
    failure_code: str | None = None
    created_at: str


class ConsultationBudgetLimits(StrictModel):
    """Limits loaded from ``config/consultation.json`` (defaults fallback).

    ``schema_version`` is part of the model so the shipped config file
    (which carries it, following the config convention) actually
    validates — an unlisted extra key would fall back to the defaults
    silently and make the file dead.
    """

    schema_version: Literal["cockpit-consultation-config-v1"] = (
        "cockpit-consultation-config-v1"
    )
    llm_calls_limit: int = DEFAULT_LLM_CALLS_LIMIT
    intervals_limit: int = DEFAULT_INTERVALS_LIMIT
    wall_seconds_limit: Annotated[float, BeforeValidator(float)] = (
        DEFAULT_WALL_SECONDS_LIMIT
    )
    preview_sample_seconds_limit: Annotated[float, BeforeValidator(float)] = (
        DEFAULT_PREVIEW_SAMPLE_SECONDS_LIMIT
    )
    # W4: per-call transport size caps (None = unenforced). Measured in
    # bytes at the consultation transport; a breach fails closed AFTER
    # recording the spent call, never by silent truncation.
    max_input_bytes_per_call: int | None = Field(default=None, ge=1)
    max_output_bytes_per_call: int | None = Field(default=None, ge=1)
    # W4: per-model call caps, enforced where the model id is known
    # (the consultation proposal path). Absent model = unattributed.
    per_model_llm_calls_limit: dict[str, int] = Field(default_factory=dict)
    # W4: the sample→full-episode boundary. Full-episode attempts draw
    # from these limits under the "full_episode" ledger scope and NEVER
    # from the sample allowance above.
    full_episode_llm_calls_limit: int = DEFAULT_LLM_CALLS_LIMIT
    full_episode_wall_seconds_limit: Annotated[float, BeforeValidator(float)] = (
        DEFAULT_WALL_SECONDS_LIMIT
    )
    # 工程4: episode-cumulative image-call cap. Image consumption folds
    # into the same llm_calls/wall counters above; this cap additionally
    # bounds image calls alone (default 6, the text-call scale).
    generation_images_limit: int = Field(default=DEFAULT_GENERATION_IMAGES_LIMIT, ge=1)


class ConsultationBudgetUsed(NamedTuple):
    llm_calls: int
    intervals: int
    wall_seconds: float


def now_stamp() -> str:
    return datetime.now(UTC).isoformat()


def _consultation_dir(episode_dir: Path) -> Path:
    return episode_dir / CONSULTATION_DIR_NAME


def _append(path: Path, record: StrictModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(record) + b"\n")


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
            raise CockpitUnprocessableError(code, f"unparsable line in {path}") from error
    return entries


def append_consultation(episode_dir: Path, record: ConsultationRecordV1) -> None:
    _append(_consultation_dir(episode_dir) / CONSULTATIONS_NAME, record)


def load_consultations(episode_dir: Path) -> list[ConsultationRecordV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / CONSULTATIONS_NAME,
        ConsultationRecordV1,
        "consultation-log-corrupt",
    )


def append_proposal_set(episode_dir: Path, proposal_set: ConsultationProposalSetV1) -> None:
    _append(_consultation_dir(episode_dir) / PROPOSALS_NAME, proposal_set)


def load_latest_proposal_sets(episode_dir: Path) -> dict[str, ConsultationProposalSetV1]:
    """Latest proposal set per consultation (regeneration supersedes)."""

    sets: dict[str, ConsultationProposalSetV1] = {}
    for saved in _load_jsonl(
        _consultation_dir(episode_dir) / PROPOSALS_NAME,
        ConsultationProposalSetV1,
        "consultation-log-corrupt",
    ):
        sets[saved.consultation_id] = saved
    return sets


def append_judgment(episode_dir: Path, judgment: ConsultationJudgmentV1) -> None:
    _append(_consultation_dir(episode_dir) / JUDGMENTS_NAME, judgment)


def load_judgments(episode_dir: Path) -> list[ConsultationJudgmentV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / JUDGMENTS_NAME,
        ConsultationJudgmentV1,
        "consultation-log-corrupt",
    )


def _join_policy(
    episode_dir: Path, judgment: ConsultationJudgmentV1
) -> AdoptedPolicyV1 | None:
    """Join one judgment with its proposal; None when not adoptable.

    Shared by the latest-policy view and the authorization binding, so
    both read the SAME proposal table the same way: adopt|revise with a
    non-empty scope, a named proposal, and that proposal still on the
    table. Anything else (withdrawals, whole-consultation judgments,
    missing proposals) yields no policy.
    """

    if judgment.decision not in ("adopt", "revise"):
        return None
    if not (
        judgment.scope.composition
        or judgment.scope.appearance
        or judgment.scope.audio
    ):
        return None
    if judgment.proposal_id is None:
        return None
    proposal_set = load_latest_proposal_sets(episode_dir).get(
        judgment.consultation_id
    )
    proposal = next(
        (
            candidate
            for candidate in (
                proposal_set.proposals if proposal_set is not None else ()
            )
            if candidate.proposal_id == judgment.proposal_id
        ),
        None,
    )
    if proposal is None:
        return None
    details = proposal.details
    return AdoptedPolicyV1(
        consultation_id=judgment.consultation_id,
        judgment_id=judgment.judgment_id,
        proposal_id=proposal.proposal_id,
        decision=judgment.decision,
        scope=judgment.scope,
        audience_message=details.audience_message,
        structure=details.structure,
        duration_estimate=details.duration_estimate,
        candidate_scenes=details.candidate_scenes,
        subtitle_policy=details.subtitle_policy,
        audio_policy=details.audio_policy,
        tempo_policy=details.tempo_policy,
        reference_mapping=details.reference_mapping,
        unused_reasons=details.unused_reasons,
        unconfirmed=details.unconfirmed,
        note=judgment.note,
        presentation_condition=details.presentation_condition,
    )


def latest_adopted_policy(episode_dir: Path) -> AdoptedPolicyV1 | None:
    """The latest adoptable judgment joined with its proposal (None = none).

    Latest judgment wins: adopt|revise with a non-empty scope and a
    resolvable proposal yields a policy; a newer reject/both_wrong, an
    empty scope, a whole-consultation judgment (no proposal), or a missing
    proposal yields None — the operator withdrew and nothing is reused.
    Never raises for journal states (corrupt lines still raise, like every
    other reader here).
    """

    judgments = load_judgments(episode_dir)
    if not judgments:
        return None
    return _join_policy(episode_dir, judgments[-1])


def policy_summary(policy: AdoptedPolicyV1) -> AdoptedPolicySummaryV1:
    """The compact director-input summary of an adopted policy."""

    return AdoptedPolicySummaryV1(
        decision=policy.decision,
        scope=AdoptedPolicyScopeV1(
            composition=policy.scope.composition,
            appearance=policy.scope.appearance,
            audio=policy.scope.audio,
        ),
        audience_message=policy.audience_message,
        structure=policy.structure,
        duration_estimate=policy.duration_estimate,
        candidate_scenes=policy.candidate_scenes,
        subtitle_policy=policy.subtitle_policy,
        audio_policy=policy.audio_policy,
        tempo_policy=policy.tempo_policy,
        reference_mapping=policy.reference_mapping,
        unused_reasons=policy.unused_reasons,
        unconfirmed=policy.unconfirmed,
        note=policy.note,
        presentation_condition=policy.presentation_condition,
    )


def policy_scope_list(policy: AdoptedPolicyV1) -> list[str]:
    return [
        name
        for name, flag in (
            ("composition", policy.scope.composition),
            ("appearance", policy.scope.appearance),
            ("audio", policy.scope.audio),
        )
        if flag
    ]


def canonical_policy_sha256(policy: AdoptedPolicyV1) -> str:
    return hashlib.sha256(canonical_model_bytes(policy)).hexdigest()


def policy_for_judgment(episode_dir: Path, judgment_id: str) -> AdoptedPolicyV1:
    judgments = load_judgments(episode_dir)
    matches = [item for item in judgments if item.judgment_id == judgment_id]
    if not matches:
        raise CockpitNotFoundError(
            "consultation-judgment-not-found",
            f"no judgment {judgment_id} in the journal",
        )
    if len(matches) > 1:
        raise CockpitUnprocessableError(
            "consultation-judgment-ambiguous",
            f"judgment {judgment_id} appears {len(matches)} times; refusing",
        )
    latest = matches[0]
    if latest.decision not in ("adopt", "revise"):
        raise CockpitUnprocessableError(
            "consultation-judgment-not-adoptable",
            f"judgment {judgment_id} decides {latest.decision}; no policy to pin",
        )
    if not (
        latest.scope.composition or latest.scope.appearance or latest.scope.audio
    ):
        raise CockpitUnprocessableError(
            "consultation-judgment-not-adoptable",
            f"judgment {judgment_id} adopts an empty scope; no policy to pin",
        )
    if latest.proposal_id is None:
        raise CockpitUnprocessableError(
            "consultation-judgment-not-adoptable",
            f"judgment {judgment_id} names no proposal; no policy to pin",
        )
    proposal_set = load_latest_proposal_sets(episode_dir).get(latest.consultation_id)
    proposal = next(
        (
            candidate
            for candidate in (proposal_set.proposals if proposal_set is not None else ())
            if candidate.proposal_id == latest.proposal_id
        ),
        None,
    )
    if proposal is None:
        raise CockpitUnprocessableError(
            "consultation-judgment-not-adoptable",
            f"judgment {judgment_id} names proposal {latest.proposal_id} "
            "which is not on the table",
        )
    details = proposal.details
    return AdoptedPolicyV1(
        consultation_id=latest.consultation_id,
        judgment_id=latest.judgment_id,
        proposal_id=proposal.proposal_id,
        decision=latest.decision,
        scope=latest.scope,
        audience_message=details.audience_message,
        structure=details.structure,
        duration_estimate=details.duration_estimate,
        candidate_scenes=details.candidate_scenes,
        subtitle_policy=details.subtitle_policy,
        audio_policy=details.audio_policy,
        tempo_policy=details.tempo_policy,
        reference_mapping=details.reference_mapping,
        unused_reasons=details.unused_reasons,
        unconfirmed=details.unconfirmed,
        note=latest.note,
        presentation_condition=details.presentation_condition,
    )


_WRITE_LOCK_NAME = ".consultation-write.lock"
_WRITE_THREAD_LOCK = threading.Lock()


@contextmanager
def consultation_write_locked(episode_dir: Path) -> Iterator[None]:
    """The single-Writer boundary for consultation check/append/reserve.

    Serializes concurrent POSTs across threads (process lock) and
    across processes (an flock'd file in the consultation dir): the
    judgment dedupe scan + append and the reservation check + append
    each run atomically inside it, so a late re-send can neither slip
    past the scan nor double-reserve.
    """

    lock_path = _consultation_dir(episode_dir) / _WRITE_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_THREAD_LOCK, lock_path.open("ab") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def judgment_operation_key(
    *,
    consultation_id: str,
    proposal_id: str | None,
    decision: ConsultationDecision,
    scope: ConsultationScope,
    note: str | None,
) -> str:
    """The stable adoption-operation key for one judgment request."""

    canon = json.dumps(
        {
            "consultation_id": consultation_id,
            "proposal_id": proposal_id,
            "decision": decision,
            "scope": {
                "composition": scope.composition,
                "appearance": scope.appearance,
                "audio": scope.audio,
            },
            "note": note,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def append_effective_judgment_once(  # noqa: PLR0913 (explicit judgment-fingerprint fields; kwargs are the contract)
    episode_dir: Path,
    *,
    consultation_id: str,
    proposal_id: str | None,
    decision: ConsultationDecision,
    scope: ConsultationScope,
    note: str | None,
    operation_id: str | None = None,
) -> tuple[ConsultationJudgmentV1, bool]:
    """Append one adoption judgment unless this operation already landed.

    Dedupe scans the WHOLE journal for the stable operation key (the
    explicit ``operation_id`` when the caller supplies one, else the
    content fingerprint) — a delayed A re-send arriving after B matches
    the original A row and reuses it, so B stays effective and no new
    row overwrites it. A deliberate re-adoption carries a NEW operation
    id and appends a new row. Scan + append hold the single-Writer lock.
    """

    key = operation_id or judgment_operation_key(
        consultation_id=consultation_id,
        proposal_id=proposal_id,
        decision=decision,
        scope=scope,
        note=note,
    )
    dedupe_applies = (
        decision in ("adopt", "revise")
        and proposal_id is not None
        and (scope.composition or scope.appearance or scope.audio)
    )
    with consultation_write_locked(episode_dir):
        judgments = load_judgments(episode_dir)
        if dedupe_applies:
            for existing in judgments:
                existing_key = existing.operation_id or judgment_operation_key(
                    consultation_id=existing.consultation_id,
                    proposal_id=existing.proposal_id,
                    decision=existing.decision,
                    scope=existing.scope,
                    note=existing.note,
                )
                if existing_key == key:
                    return existing, False
        judgment = ConsultationJudgmentV1(
            judgment_id=uuid.uuid4().hex[:12],
            consultation_id=consultation_id,
            proposal_id=proposal_id,
            decision=decision,
            scope=scope,
            note=note,
            operation_id=operation_id,
            created_at=now_stamp(),
        )
        append_judgment(episode_dir, judgment)
        return judgment, True


def append_policy_outcome(episode_dir: Path, outcome: ConsultationPolicyOutcomeV1) -> None:
    _append(_consultation_dir(episode_dir) / OUTCOMES_NAME, outcome)


def append_policy_outcome_once(
    episode_dir: Path, outcome: ConsultationPolicyOutcomeV1
) -> tuple[ConsultationPolicyOutcomeV1, bool]:
    outcomes = load_policy_outcomes(episode_dir)
    if outcomes:
        latest = outcomes[-1]
        if (
            latest.judgment_id == outcome.judgment_id
            and latest.reservation_sequence == outcome.reservation_sequence
            and latest.status == outcome.status
            and latest.commit_event_id == outcome.commit_event_id
            and latest.failure_code == outcome.failure_code
        ):
            return latest, False
    append_policy_outcome(episode_dir, outcome)
    return outcome, True


def load_policy_outcomes(episode_dir: Path) -> list[ConsultationPolicyOutcomeV1]:
    return _load_jsonl(
        _consultation_dir(episode_dir) / OUTCOMES_NAME,
        ConsultationPolicyOutcomeV1,
        "consultation-log-corrupt",
    )


def load_policy_rebuild_entries(episode_dir: Path) -> list[RebuildRequestEntry]:
    """Judgment-linked rebuild entries, tolerant like the status view."""

    try:
        lines = (episode_dir / REBUILD_LOG_NAME).read_bytes().splitlines()
    except OSError:
        return []
    entries: list[RebuildRequestEntry] = []
    for line in lines:
        try:
            entries.append(RebuildRequestEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def _selection_run_state(
    stage_runs: Sequence[StageRunRow], run_id: str | None
) -> Literal["failed", "succeeded", "running", "requested"]:
    """State of one spawned selection rebuild from cockpit stage rows.

    ``requested`` means spawned but with no sign of life yet (no stage
    rows for the run) — the reservation is the only evidence. Only actual
    rows promote it to running/succeeded/failed.
    """

    if run_id is None:
        return "requested"
    seen = False
    failed = False
    preview_done = False
    for row in stage_runs:
        if row.run_id != run_id:
            continue
        seen = True
        if row.status == "failed_blocked":
            failed = True
        if row.stage_name == "preview" and row.status == "succeeded":
            preview_done = True
    if failed:
        return "failed"
    if preview_done:
        return "succeeded"
    if seen:
        return "running"
    return "requested"


def derive_policy_rebuild(  # noqa: PLR0911 (one return per rebuild state; the state table)
    episode_dir: Path,
    policy: AdoptedPolicyV1 | None,
    stage_runs: Sequence[StageRunRow] = (),
) -> dict[str, object]:
    """The consultation view's ``rebuild`` object for one adopted policy.

    Derived deterministically: a reservation carrying this policy's
    judgment is ``requested``; a spawned run is ``running``/``succeeded``/
    ``failed`` from its cockpit stage rows; a failed outcome (and no later
    honored one) is ``failed`` with the honest reason. ``detail`` never
    claims beyond what was verified.
    """

    if policy is None:
        linked: list[RebuildRequestEntry] = []
        outcomes: list[ConsultationPolicyOutcomeV1] = []
    else:
        linked = [
            entry
            for entry in load_policy_rebuild_entries(episode_dir)
            if entry.judgment_id == policy.judgment_id
        ]
        outcomes = [
            outcome
            for outcome in load_policy_outcomes(episode_dir)
            if outcome.judgment_id == policy.judgment_id
        ]
    honored = [
        outcome for outcome in outcomes
        if outcome.status in ("honored", "connected")
    ]
    if outcomes and outcomes[-1].status == "failed":
        last = outcomes[-1]
        detail = "; ".join(last.reasons) or last.note or "方針の反映に失敗しました。"
        return {"status": "failed", "target_version": last.plan_version, "detail": detail}
    target = honored[-1].plan_version if honored else None
    if linked and linked[-1].failure_code is not None:
        terminal = linked[-1]
        return {
            "status": "failed",
            "target_version": target,
            "detail": terminal.detail or "再生成の起動に失敗しました。",
        }
    spawns = [entry for entry in linked if entry.spawned]
    if not linked:
        return {
            "status": "none",
            "target_version": target,
            "detail": (
                "採用中の方針はありません。"
                if policy is None
                else "この方針の再生成は要求されていません。"
            ),
        }
    state = _selection_run_state(stage_runs, spawns[-1].run_id) if spawns else "requested"
    if state == "requested":
        return {
            "status": "requested",
            "target_version": target,
            "detail": (
                "再生成を起動しました。実行開始待ち。"
                if spawns
                else "再生成待ち (起動予約済み)。"
            ),
        }
    if state == "failed":
        return {
            "status": "failed",
            "target_version": target,
            "detail": "再生成の実行が失敗しました。相談へ戻って方針を見直せます。",
        }
    if state == "succeeded":
        return {
            "status": "succeeded",
            "target_version": target,
            "detail": (
                "反映の検証は構造検証のみ。"
                if honored
                else "再生成は完了しましたが方針の確定記録がありません。"
            ),
        }
    return {
        "status": "running",
        "target_version": target,
        "detail": (
            "方針を反映した版を生成中です。反映の検証は構造検証のみ。"
            if honored
            else "再生成を実行中です。"
        ),
    }


def consultation_adoption_open(episode_dir: Path) -> bool:
    """Whether an adoption stands open (a newer withdrawal closes it).

    Trailing 全編へ rows neither adopt nor withdraw: the adoption they
    stand on keeps the consultation open until a newer adoption decision
    or withdrawal moves it. A legacy or expired authorization therefore
    never silently returns the flow to ordinary routing — the sample
    path stays the preview surface until a BOUND authorization holds.
    """

    judgments = load_judgments(episode_dir)
    prefix = list(judgments)
    while prefix and prefix[-1].decision == "full_authorized":
        prefix.pop()
    if not prefix:
        return False
    return _join_policy(episode_dir, prefix[-1]) is not None


def selection_rebuild_active(
    episode_dir: Path, stage_runs: Sequence[StageRunRow] = ()
) -> bool:
    """True while a judgment-linked selection rebuild is still unresolved.

    Orphan reservations (spawn never happened) do NOT block: a subsequent
    schedule supersedes them and the latest policy is what it runs. A
    spawned run counts as active until its stage rows prove a terminal
    state — a spawn with no rows yet is presumed alive, never double
    scheduled.
    """

    linked = [
        entry
        for entry in load_policy_rebuild_entries(episode_dir)
        if entry.judgment_id is not None
    ]
    spawns = [entry for entry in linked if entry.spawned]
    if not spawns:
        return False
    return _selection_run_state(stage_runs, spawns[-1].run_id) in (
        "running",
        "requested",
    )


def append_budget_event(episode_dir: Path, event: ConsultationBudgetEventV1) -> None:
    _append(_consultation_dir(episode_dir) / BUDGET_NAME, event)


def budget_used(episode_dir: Path) -> ConsultationBudgetUsed:
    """Cumulative counters — the sum of the WHOLE budget journal (no reset)."""

    events = _load_jsonl(
        _consultation_dir(episode_dir) / BUDGET_NAME,
        ConsultationBudgetEventV1,
        "consultation-log-corrupt",
    )
    return ConsultationBudgetUsed(
        llm_calls=sum(event.llm_calls for event in events),
        intervals=sum(event.intervals for event in events),
        wall_seconds=sum(event.wall_seconds for event in events),
    )


def consume_budget(  # noqa: PLR0913 (explicit budget-line fields; kwargs are the ledger contract)
    episode_dir: Path,
    consultation_id: str,
    *,
    llm_calls: int,
    intervals: int,
    wall_seconds: float,
    model_id: str | None = None,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
    retry_index: int = 0,
    failure_code: str | None = None,
    kind: BudgetEventKind = "text",
) -> None:
    """Record one generation's spend — successes AND failures alike."""

    append_budget_event(
        episode_dir,
        ConsultationBudgetEventV1(
            consultation_id=consultation_id,
            llm_calls=llm_calls,
            intervals=intervals,
            wall_seconds=wall_seconds,
            model_id=model_id,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            retry_index=retry_index,
            failure_code=failure_code,
            kind=kind,
            created_at=now_stamp(),
        ),
    )


def image_calls_used(episode_dir: Path) -> int:
    """Cumulative image transport calls (scope-tagged slice of budget_used)."""

    return sum(
        event.llm_calls
        for event in _load_jsonl(
            _consultation_dir(episode_dir) / BUDGET_NAME,
            ConsultationBudgetEventV1,
            "consultation-log-corrupt",
        )
        if event.kind == "image"
    )


def ensure_image_budget_available(
    episode_dir: Path, limits: ConsultationBudgetLimits, needed: int
) -> None:
    """Typed 422 BEFORE starting image calls past the episode image cap."""

    if image_calls_used(episode_dir) + needed > limits.generation_images_limit:
        raise CockpitUnprocessableError(
            "consultation-budget-exhausted",
            f"consultation image budget exhausted: {image_calls_used(episode_dir)}/"
            f"{limits.generation_images_limit} image calls used, {needed} requested",
        )


def panel_image_path(episode_dir: Path, panel_id: str) -> Path:
    """The generated file reference for one panel (episode-relative honest)."""

    return _consultation_dir(episode_dir) / PANELS_DIR_NAME / f"{panel_id}.png"


def append_panel(episode_dir: Path, panel: ConsultationPanelV1) -> None:
    _append(_consultation_dir(episode_dir) / PANELS_NAME, panel)


def load_panels(
    episode_dir: Path, consultation_id: str
) -> list[ConsultationPanelV1]:
    """Latest record per panel_id (append-only; a retry appends, never edits)."""

    latest: dict[str, ConsultationPanelV1] = {}
    for saved in _load_jsonl(
        _consultation_dir(episode_dir) / PANELS_NAME,
        ConsultationPanelV1,
        "consultation-log-corrupt",
    ):
        if saved.consultation_id == consultation_id:
            latest[saved.panel_id] = saved
    return list(latest.values())


def append_generation_event(episode_dir: Path, event: GenerationEventV1) -> None:
    _append(_consultation_dir(episode_dir) / GENERATION_NAME, event)


def load_generation_events(
    episode_dir: Path, consultation_id: str
) -> list[GenerationEventV1]:
    return [
        saved
        for saved in _load_jsonl(
            _consultation_dir(episode_dir) / GENERATION_NAME,
            GenerationEventV1,
            "consultation-log-corrupt",
        )
        if saved.consultation_id == consultation_id
    ]


def panel_kind_of(role: PanelRole) -> tuple[str, str]:
    """The U35 区分 labels: (kind, Japanese label) for one panel role."""

    if role == "real_frame":
        return ("real_frame", "撮影素材")
    return ("generated", "生成した見た目の案")


def panel_view(panel: ConsultationPanelV1) -> dict[str, object]:
    """One panel's wire object (区分 labels + status + base pin)."""

    kind, kind_label = panel_kind_of(panel.role)
    return {
        "panel_id": panel.panel_id,
        "proposal_id": panel.proposal_id,
        "role": panel.role,
        "kind": kind,
        "kind_label_ja": kind_label,
        "source_frame_ref": panel.source_frame_ref,
        "image_ref": panel.image_ref,
        "caption": panel.caption.model_dump(mode="json"),
        "status": panel.status,
        "error_code": panel.error_code,
        "model_selected": panel.model_selected,
        "base_created_at": panel.base_created_at,
        "created_at": panel.created_at,
    }


def ensure_budget_available(episode_dir: Path, limits: ConsultationBudgetLimits) -> None:
    """Typed 422 BEFORE any LLM call when any limit would be exceeded."""

    used = budget_used(episode_dir)
    exhausted = (
        used.llm_calls + 1 > limits.llm_calls_limit
        or used.intervals + 1 > limits.intervals_limit
        or used.wall_seconds >= limits.wall_seconds_limit
    )
    if exhausted:
        raise CockpitUnprocessableError(
            "consultation-budget-exhausted",
            f"consultation budget exhausted: {used.llm_calls}/"
            f"{limits.llm_calls_limit} LLM calls, {used.intervals}/"
            f"{limits.intervals_limit} intervals, {used.wall_seconds:.1f}/"
            f"{limits.wall_seconds_limit:.1f}s wall time used",
        )


def combined_llm_used(episode_dir: Path) -> int:
    return budget_used(episode_dir).llm_calls + selection_budget_used(episode_dir).llm_calls


def combined_wall_used(episode_dir: Path) -> float:
    return combined_wall_used_in_scope(episode_dir, "sample")


def combined_wall_used_in_scope(episode_dir: Path, *scopes: BudgetScope) -> float:
    """Proposal-journal wall + the named ledger scopes' wall seconds.

    ``combined_wall_used`` is exactly this over the "sample" scope — the
    deadline fold also reads the unauthorized historical
    "full_rebuild_exempt" rows (read for budget fold compatibility; no
    new writes), so their wall seconds stay wall-bounded (the wall
    deadline enforcement is unchanged; only the SAMPLE cap is scoped).
    """

    wall = budget_used(episode_dir).wall_seconds
    for scope in scopes:
        wall += selection_budget_used_in_scope(episode_dir, scope).wall_seconds
    return wall


def remaining_wall_seconds(episode_dir: Path, limits: ConsultationBudgetLimits) -> float:
    return limits.wall_seconds_limit - combined_wall_used(episode_dir)


def ensure_selection_budget_available(
    episode_dir: Path, limits: ConsultationBudgetLimits
) -> float:
    """Typed 422 BEFORE a selection director call; returns remaining wall.

    Proposal generation and selection rebuilds share one cumulative
    episode budget: one call plus the full live-director wall allowance
    must remain, or the director is never contacted (failures still
    settle their measured usage, so the spend stays visible).
    """

    remaining = remaining_wall_seconds(episode_dir, limits)
    if (
        combined_llm_used(episode_dir) + 1 > limits.llm_calls_limit
        or remaining < DIRECTOR_WALL_ALLOWANCE_SECONDS
    ):
        raise CockpitUnprocessableError(
            "consultation-selection-budget-exhausted",
            "この相談で使えるAI回数または処理時間の上限に達したため、"
            "作り直しを始めませんでした。",
        )
    return remaining


def ensure_preview_budget_available(
    episode_dir: Path, limits: ConsultationBudgetLimits, preview_seconds: float
) -> None:
    """Typed 422 BEFORE committing or rendering an over-budget sample.

    Fail-closed: an over-allowance plan is never silently truncated to
    fit — the bundle stays whole-plan-bound and the run stops. This is
    the SAMPLE gate: it folds the "sample" ledger scope only.
    Unauthorized historical "full_rebuild_exempt" rows (read for budget
    fold compatibility; no new writes) never enter this gate, while
    their wall time stays under the unchanged wall deadline.
    """

    used = selection_budget_used(episode_dir).preview_seconds
    if used + preview_seconds > limits.preview_sample_seconds_limit:
        raise CockpitUnprocessableError(
            "consultation-preview-budget-exhausted",
            "見本映像は合計30秒の上限を超えるため、映像生成を始めませんでした。",
        )


def model_attributed_calls(episode_dir: Path, model_id: str) -> int:
    """LLM calls attributed to one model across both ledgers."""

    from services.episode_cockpit.consultation_selection_budget import (  # noqa: PLC0415 (deferred: store owns the combined view)
        load_selection_budget_entries,
    )

    proposal = sum(
        event.llm_calls
        for event in _load_jsonl(
            _consultation_dir(episode_dir) / BUDGET_NAME,
            ConsultationBudgetEventV1,
            "consultation-log-corrupt",
        )
        if event.model_id == model_id
    )
    ledger = sum(
        entry.llm_calls_used
        for entry in load_selection_budget_entries(episode_dir)
        if entry.model_id == model_id and entry.phase == "director_settled"
    )
    return proposal + ledger


def ensure_model_call_budget_available(
    episode_dir: Path, limits: ConsultationBudgetLimits, model_id: str | None
) -> None:
    """Typed 422 BEFORE contacting a model past its per-model call cap.

    Unconfigured models (no cap for this id) and unattributed calls
    (model unknown) pass — the cap only binds what it names.
    """

    if model_id is None:
        return
    cap = limits.per_model_llm_calls_limit.get(model_id)
    if cap is None:
        return
    if model_attributed_calls(episode_dir, model_id) + 1 > cap:
        raise CockpitUnprocessableError(
            "consultation-model-budget-exhausted",
            f"model {model_id} reached its per-model call cap ({cap}); "
            "no further calls are started for it.",
        )


def ensure_full_episode_budget_available(
    episode_dir: Path, limits: ConsultationBudgetLimits
) -> None:
    """Typed 422 BEFORE a full-episode attempt past its own allowance.

    The sample→full boundary: full-episode attempts draw ONLY from the
    full-episode limits under the "full_episode" ledger scope — the
    sample allowance is never touched by them, and the sample gate
    never counts them.
    """

    from services.episode_cockpit.consultation_selection_budget import (  # noqa: PLC0415 (deferred: store owns the combined view)
        selection_budget_used_in_scope,
    )

    used = selection_budget_used_in_scope(episode_dir, "full_episode")
    if (
        used.llm_calls + 1 > limits.full_episode_llm_calls_limit
        or used.wall_seconds >= limits.full_episode_wall_seconds_limit
    ):
        raise CockpitUnprocessableError(
            "consultation-full-episode-budget-exhausted",
            "全編処理の予算上限に達したため、全編の処理を始めませんでした。"
            "見本の予算とは別枠です。",
        )


def load_budget_limits(config_path: Path | None = None) -> ConsultationBudgetLimits:
    """Tolerant config read: absent/malformed file → the pinned defaults."""

    path = config_path if config_path is not None else _CONFIG_ROOT / CONFIG_RELATIVE
    try:
        parsed: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return ConsultationBudgetLimits()
    if not isinstance(parsed, dict):
        return ConsultationBudgetLimits()
    try:
        return ConsultationBudgetLimits.model_validate(parsed)
    except ValidationError:
        return ConsultationBudgetLimits()


def require_consultation(episode_dir: Path, consultation_id: str) -> ConsultationRecordV1:
    for record in load_consultations(episode_dir):
        if record.consultation_id == consultation_id:
            return record
    raise CockpitNotFoundError(
        "consultation-not-found", f"no consultation {consultation_id} in the journal"
    )


def require_proposal(
    episode_dir: Path, consultation_id: str, proposal_id: str
) -> ConsultationProposalV1:
    proposal_set = load_latest_proposal_sets(episode_dir).get(consultation_id)
    if proposal_set is not None:
        for proposal in proposal_set.proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
    raise CockpitUnprocessableError(
        "consultation-proposal-not-found",
        f"consultation {consultation_id} has no proposal {proposal_id} on the table",
    )


def consultation_view(
    episode_dir: Path,
    record: ConsultationRecordV1,
    limits: ConsultationBudgetLimits,
    *,
    snapshot: JobSnapshot | None = None,
) -> dict[str, object]:
    """The pinned consultation object (no schema_version noise on the wire)."""

    proposal_set = load_latest_proposal_sets(episode_dir).get(record.consultation_id)
    judgments = [
        judgment
        for judgment in load_judgments(episode_dir)
        if judgment.consultation_id == record.consultation_id
    ]
    used = budget_used(episode_dir)
    selection = selection_budget_used(episode_dir)
    policy = latest_adopted_policy(episode_dir)
    stage_runs = snapshot.stage_runs if snapshot is not None else ()
    outcomes = [
        {**outcome.model_dump(mode="json"), "kind": "policy_outcome"}
        for outcome in load_policy_outcomes(episode_dir)
        if outcome.consultation_id == record.consultation_id
    ]
    budget: dict[str, object] = {
        "llm_calls_used": used.llm_calls + selection.llm_calls,
        "llm_calls_limit": limits.llm_calls_limit,
        "intervals_used": used.intervals,
        "intervals_limit": limits.intervals_limit,
        "wall_seconds_used": used.wall_seconds + selection.wall_seconds,
        "wall_seconds_limit": limits.wall_seconds_limit,
        "preview_seconds_used": selection.preview_seconds,
        "preview_sample_seconds_limit": limits.preview_sample_seconds_limit,
        "cost_display": "unmeasured",
    }
    view: dict[str, object] = {
        "consultation_id": record.consultation_id,
        "created_at": record.created_at,
        "message": record.message,
        "proposals": [
            proposal.model_dump(mode="json")
            for proposal in (proposal_set.proposals if proposal_set else ())
        ],
        "judgments": [
            judgment.model_dump(
                mode="json", exclude={"schema_version", "consultation_id"}
            )
            for judgment in judgments
        ],
        "budget": budget,
        "policy": {
            "adopted": policy.model_dump(mode="json") if policy is not None else None
        },
        "rebuild": derive_policy_rebuild(episode_dir, policy, stage_runs),
        "policy_outcomes": outcomes,
    }
    # 工程4: generation keys ride ONLY consultations with panel/event
    # records — every other view stays byte-identical to today's shape.
    panels = load_panels(episode_dir, record.consultation_id)
    events = load_generation_events(episode_dir, record.consultation_id)
    if not panels and not events:
        return view
    budget["image_calls_used"] = image_calls_used(episode_dir)
    budget["generation_images_limit"] = limits.generation_images_limit
    view["panels"] = [panel_view(panel) for panel in panels]
    latest = events[-1]
    view["generation"] = {
        "model_selected": latest.model_selected,
        "verified": latest.verified,
        "decision": latest.decision,
        "reason": latest.reason,
        "note": latest.note,
    }
    return view


__all__ = [
    "BUDGET_NAME",
    "CONFIG_RELATIVE",
    "CONNECTED_POLICY_FIELDS",
    "CONSULTATIONS_NAME",
    "CONSULTATION_DIR_NAME",
    "DEFAULT_GENERATION_IMAGES_LIMIT",
    "DEFAULT_INTERVALS_LIMIT",
    "DEFAULT_LLM_CALLS_LIMIT",
    "DEFAULT_PREVIEW_SAMPLE_SECONDS_LIMIT",
    "DEFAULT_WALL_SECONDS_LIMIT",
    "GENERATION_NAME",
    "JUDGMENTS_NAME",
    "MAX_GENERATION_PROPOSALS",
    "MAX_PANELS_PER_PROPOSAL",
    "OUTCOMES_NAME",
    "PANELS_DIR_NAME",
    "PANELS_NAME",
    "POLICY_SCOPE_ORDER",
    "PROPOSALS_NAME",
    "REBUILD_LOG_NAME",
    "SCOPE_UNADDRESSED_JA",
    "STRUCTURAL_REALIZED_CHECKS",
    "TRIAL_VIEW_UNCONFIRMED_JA",
    "AdoptedPolicyV1",
    "BudgetEventKind",
    "ConsultationBudgetEventV1",
    "ConsultationBudgetLimits",
    "ConsultationBudgetUsed",
    "ConsultationDecision",
    "ConsultationJudgmentV1",
    "ConsultationPanelV1",
    "ConsultationPolicyOutcomeV1",
    "ConsultationProposalDetails",
    "ConsultationProposalSetV1",
    "ConsultationProposalV1",
    "ConsultationRecordV1",
    "ConsultationScope",
    "DirectorConnection",
    "FullAuthorizationBinding",
    "GenerationDecision",
    "GenerationEventV1",
    "GenerationPermissionV1",
    "PanelCaptionV1",
    "PanelRole",
    "PanelStatus",
    "PolicyOutcomeStatus",
    "PolicySettingEntryV1",
    "append_budget_event",
    "append_consultation",
    "append_effective_judgment_once",
    "append_full_authorization_once",
    "append_generation_event",
    "append_judgment",
    "append_panel",
    "append_policy_outcome",
    "append_policy_outcome_once",
    "append_proposal_set",
    "budget_used",
    "canonical_policy_sha256",
    "combined_llm_used",
    "combined_wall_used",
    "combined_wall_used_in_scope",
    "consultation_adoption_open",
    "consultation_view",
    "consultation_write_locked",
    "consume_budget",
    "derive_policy_rebuild",
    "ensure_budget_available",
    "ensure_full_episode_budget_available",
    "ensure_image_budget_available",
    "ensure_model_call_budget_available",
    "ensure_preview_budget_available",
    "ensure_selection_budget_available",
    "full_authorization_content_key",
    "full_render_authorized",
    "image_calls_used",
    "judgment_operation_key",
    "latest_adopted_policy",
    "load_budget_limits",
    "load_consultations",
    "load_generation_events",
    "load_judgments",
    "load_latest_proposal_sets",
    "load_panels",
    "load_policy_outcomes",
    "load_policy_rebuild_entries",
    "model_attributed_calls",
    "now_stamp",
    "panel_image_path",
    "panel_kind_of",
    "panel_view",
    "policy_for_judgment",
    "policy_scope_list",
    "policy_summary",
    "remaining_wall_seconds",
    "require_consultation",
    "require_proposal",
    "selection_rebuild_active",
    "unaddressed_for_scope",
    "unconfirmed_for_policy",
]
