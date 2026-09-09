"""The real-episode Editorial Director: two honestly-labeled modes (Todo 46).

DETERMINISTIC BASELINE (no live route): keep-all-speech — every speech segment
selected, must-include all, no removals — assembled through the Todo-40
candidate machinery with producer ``deterministic-baseline-v1`` and an
explicit no-model-involved marker; zero model calls, zero network. LIVE
(``EDITORIAL_DIRECTOR_API_KEY`` set): the Todo-39 adapter over the stdlib
urllib transport, bounded evidence via the Todo-37 MediaQueryApi over the
REAL index, transcript transport authorized by the Todo-12 gate over the
resolved production policy (deny-by-default); a live refusal is honest
failure — the chain NEVER silently falls back.

FLAT-RATE LIVE (runtime ``production_model`` + ``codex-exec``): the SAME
Todo-39 adapter over the pinned ``codex exec`` transport
(``CodexEditorialTransport``) — the same ``DirectorRequest`` in, the same
``parse_response`` validation downstream, so selection semantics are
identical to the metered path; only the transport wrapper differs. The model
id rides the runner call from the director pin. Transport precedence: the
resolved editorial runtime decides (explicit ``runtime_path`` arg >
``EDITORIAL_RUNTIME_CONFIG`` env > repo-default
``config/editorial-runtime.json``); the openai-api key alone never diverts a
codex-exec runtime and codex-exec never fires under a heuristic runtime —
never silently mixed. Note this resolution is the director seam's own: the
cockpit runner's ``editorial_mode`` gate (missing config means diagnostic)
is separate and untouched — pass the same runtime file via
``--editorial-runtime``/``EDITORIAL_RUNTIME_CONFIG`` to keep the two
consistent.

BUDGET: the director's codex-exec call is the chain's own cost (like the
analyze stage) — it is NOT billed to the consultation budget, which covers
the consultation LLM only. The outcome records which transport/model served
(``served_by`` + ``transport``, journaled into run-report.json) mirroring
the consultation GenerationCall honesty — no false live claims. The single
bounded contradiction retry appends one ``director-calls.jsonl`` ledger
line per completed call (attempt 1, then attempt 2 only on retry), so both
calls are accounted even when the retry also fails validation.

RETRY: when the semantic validator refuses a proposal with ONLY
``keep_remove_contradiction`` (the r8 measured blocker: one span carrying
both keep and remove intents without a parent relation), the director is
re-invoked EXACTLY ONCE with the refusal appended to the original request
(``refusal_feedback`` rides the prompt bundle as DATA). Any other refusal
(schema, other semantic codes, lock, capability, contract) never retries —
it flows to the commit authority unchanged. A twice-refused proposal
propagates the same typed refusal with nothing committed and no third
attempt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.cli.director_call_ledger import (
    DirectorCallEntryV1,
    append_director_call,
    now_stamp,
)
from services.cli.live_editorial import LiveHttpTransport
from services.cli.live_editorial_codex import (
    CodexEditorialTransport,
    CodexTransportGatedError,
)
from services.cli.real_director_runtime import (
    DirectorMode,
    DirectorRoute,
    DirectorTransport,
    RealDirectorError,
    resolve_director_route,
)
from services.cli.real_policy import load_policy, write_policy_snapshot
from services.cli.real_pool import evidence_index_for, rules_for
from services.cli.real_selection_inputs import save_selection_inputs
from services.contracts.editorial_model import (
    EditorialSelectionProposal,
    SelectionEntry,
)
from services.editorial.candidate_models import (
    CandidatePool,
    EvidenceIndex,
    ProposalProducer,
    SelectionPlanProposal,
)
from services.editorial.director import EditorialDirector
from services.editorial.models import (
    AdoptedPolicySummaryV1,
    DeclaredCandidate,
    DirectorRequest,
)
from services.editorial.pin import load_pin
from services.editorial.policy import decide_production_transport_policy
from services.editorial.proposal_builder import build_selection_proposal
from services.editorial.reconcile import reconcile
from services.fixtures.manifest_phase1 import EditorialRules, EditSourceSpec
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.media_query.api import MediaQueryApi
from services.policy.redaction import redact_text
from services.validate.selection_models import ValidationContext
from services.validate.selection_semantic import validate_semantic

if TYPE_CHECKING:
    from services.cli.real_analyze import RealAnalysis
    from services.cli.real_pool import SpeechSegment
    from services.config.models import ResolvedConfig
    from services.editorial.pin import EditorialDirectorPin
    from services.editorial.reconcile import ReconciliationResult
    from services.editorial.transport import EditorialTransport
    from services.episode_cockpit.models import EpisodeEditorialGrantV1
    from services.validate.selection_models import (
        CommittedEpisodeRecord,
        EditSourceFacts,
        ValidationRefusal,
    )

BASELINE_PRODUCER = ProposalProducer(
    model_role_id="deterministic-baseline-v1",
    contract_version="deterministic-baseline-v1:no-model-involved",
)
LIVE_PRODUCER = ProposalProducer(
    model_role_id="editorial-director",
    contract_version="phase-1-editorial-director-v1",
)
NEUTRAL_SCORE = 5


def director_mode(env: dict[str, str], runtime_path: Path | None = None) -> DirectorMode:
    return resolve_director_route(env, runtime_path).mode


RESIDUAL_SECRET_RE: Final = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}\b"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\d{7,}"
)


def redacted_speech_text(speech_text: dict[str, str]) -> dict[str, str]:
    """Redact every transcript string BEFORE any prompt construction.

    A live cloud grant may only ever see the redacted text; when sensitive
    content SURVIVES redaction (a bare credential shape the deterministic
    redactor cannot safely rewrite), the live transport is denied outright.
    """

    cleaned = {segment_id: redact_text(text) for segment_id, text in speech_text.items()}
    for segment_id, text in cleaned.items():
        if RESIDUAL_SECRET_RE.search(text):
            raise RealDirectorError(
                "transcript-redaction-ineffective",
                f"segment {segment_id} still carries sensitive content after "
                "redaction; the live editorial transport is denied rather "
                "than shipping it to the cloud prompt",
            )
    return cleaned


#: Operator-facing guidance appended (only) to a production-policy
#: ``local_only_denial``: the gate still denies with zero model calls — the
#: text just names the missing grant and how to declare it. The fixture-
#: binding denial path is untouched.
GRANT_MISSING_GUIDANCE: Final = (
    "実素材の transcript を編集長(codex-exec)へ送るには、操作者の宣言"
    "(editorial-grant: transcript→editorial_direct)が必要です。"
    "POST /episodes/{episode_id}/editorial-grant に "
    '{"granted": true, "note": "<用途メモ>"} を送って宣言してください。'
    "宣言なしでは local_only のまま model 呼び出しは行いません。"
)


@dataclass(frozen=True, slots=True)
class DirectorOutcome:
    mode: DirectorMode
    proposal: EditorialSelectionProposal
    request_hash: str
    served_by: str
    transport: DirectorTransport = "deterministic-baseline"
    attempts_made: int = 1
    first_refusal: str | None = None


#: The only semantic refusal code that earns exactly one director retry.
CONTRADICTION_RETRY_CODE: Final = "keep_remove_contradiction"


def refusal_feedback_text(reason: str) -> str:
    """The refusal appendage for the single bounded contradiction retry."""

    return (
        f"前回の提案は次の理由で確定を拒否されました: {reason}。"
        "制約を守って再提案してください。"
    )


def is_contradiction_refusal(refusal: ValidationRefusal) -> bool:
    """Whether this refusal is the retryable semantic contradiction (and only it)."""

    return refusal.validator == "semantic" and refusal.code == CONTRADICTION_RETRY_CODE


@dataclass(frozen=True, slots=True)
class SelectionRetryAttempt:
    """One validated director attempt: its call identity plus its refusal, if any."""

    attempt: int
    request_hash: str
    served_by: str
    refusal_code: str | None
    refusal_detail: str | None


@dataclass(frozen=True, slots=True)
class ContradictionRetryInput:
    """The single grouped input for the bounded contradiction retry."""

    request: DirectorRequest
    index_path: str
    policy: ResolvedConfig
    env: dict[str, str]
    pool: CandidatePool
    evidence_index: EvidenceIndex
    rules: EditorialRules
    speech_ids: tuple[str, ...]
    episode: CommittedEpisodeRecord
    facts: EditSourceFacts
    ledger_dir: Path | None = None
    runtime_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ContradictionRetryResult:
    """The final outcome/proposal plus the honest per-attempt record."""

    outcome: DirectorOutcome
    selection: SelectionPlanProposal
    attempts: tuple[SelectionRetryAttempt, ...]


def director_request(  # noqa: PLR0913 (declared-candidate table from the real pool)
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    rules: EditorialRules,
    pool: CandidatePool,
    speech_text: dict[str, str],
    adopted_policy: AdoptedPolicySummaryV1 | None = None,
    refusal_feedback: str | None = None,
) -> DirectorRequest:
    return DirectorRequest(
        episode_id=episode_id,
        edit_source=EditSourceSpec(
            source_id=source_id,
            frame_rate_num=30,
            frame_rate_den=1,
            total_frames=total_frames,
            audio_sample_rate=48000,
        ),
        rules=rules,
        candidates=tuple(
            DeclaredCandidate(
                segment_id=record.segment_id,
                kind=record.kind,
                text=speech_text.get(record.segment_id, ""),
                start_frame=record.span.start_frame,
                end_frame=record.span.end_frame,
                content_score=NEUTRAL_SCORE,
                clarity_score=NEUTRAL_SCORE,
                pause_ms=0,
            )
            for record in pool.segments
        ),
        adopted_policy=adopted_policy,
        refusal_feedback=refusal_feedback,
    )


def _baseline_document(
    episode_id: str, speech_ids: tuple[str, ...]
) -> EditorialSelectionProposal:
    return EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id=f"sel-baseline-{episode_id}-v1",
        episode_id=episode_id,
        actor_intent="model",
        selection=tuple(
            SelectionEntry(segment_id=sid, action="selected",
                           reason_code="keep-all-speech-baseline")
            for sid in speech_ids
        ),
        confidence=(1, 1),
    )


def _serve(  # noqa: PLR0913 (live assembly: request/index/policy/transport + serve records)
    request: DirectorRequest,
    index_path: str,
    policy: ResolvedConfig,
    transport: EditorialTransport,
    *,
    transport_kind: Literal["live-http", "live-codex-exec"],
    served_by_fallback: str,
    served_transport: DirectorTransport,
) -> DirectorOutcome:
    pin: EditorialDirectorPin = load_pin()
    director = EditorialDirector(
        transport=transport,
        pin=pin,
        policy_decider=lambda episode: decide_production_transport_policy(episode, policy),
        transport_kind=transport_kind,
    )
    with MediaQueryApi.open(Path(index_path)) as api:
        result = director.run(request, api=api)
    if result.error is not None:
        detail = result.error.detail
        if (
            result.error.code == "local_only_denial"
            and result.policy.binding_scope == "production-policy"
        ):
            detail = f"{detail} {GRANT_MISSING_GUIDANCE}"
        raise RealDirectorError(
            "director_failed",
            f"{result.envelope.status}: {detail} "
            f"(transport_code={result.error.transport_code})",
        )
    if result.proposal is None:
        raise RealDirectorError(
            "director_refused", str(result.refusal_reason or "no proposal emitted")
        )
    return DirectorOutcome(
        mode="live",
        proposal=result.proposal,
        request_hash=result.envelope.request_hash,
        served_by=result.metadata.observed_model or served_by_fallback,
        transport=served_transport,
    )


def _live(
    request: DirectorRequest, index_path: str, policy: ResolvedConfig, env: dict[str, str]
) -> DirectorOutcome:
    pin = load_pin()
    return _serve(
        request,
        index_path,
        policy,
        LiveHttpTransport(request=request, pin=pin, env=env),
        transport_kind="live-http",
        served_by_fallback=f"live:{pin.model_id}",
        served_transport="openai-api",
    )


def _live_codex(
    request: DirectorRequest, index_path: str, policy: ResolvedConfig
) -> DirectorOutcome:
    pin = load_pin()
    try:
        transport = CodexEditorialTransport(request=request, pin=pin)
    except CodexTransportGatedError as error:
        raise RealDirectorError(error.code, error.detail) from error
    return _serve(
        request,
        index_path,
        policy,
        transport,
        transport_kind="live-codex-exec",
        served_by_fallback=f"codex-exec:{pin.model_id}",
        served_transport="codex-exec",
    )


def _direct_request(
    request: DirectorRequest,
    route: DirectorRoute,
    *,
    index_path: str,
    policy: ResolvedConfig,
    env: dict[str, str],
) -> DirectorOutcome:
    if route.transport == "codex-exec":
        return _live_codex(request, index_path, policy)
    return _live(request, index_path, policy, env)


def select(  # noqa: PLR0913 (director wiring: pool + rules + policy + env + evidence)
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    rules: EditorialRules,
    pool: CandidatePool,
    speech_ids: tuple[str, ...],
    speech_text: dict[str, str],
    index_path: str,
    policy: ResolvedConfig,
    env: dict[str, str],
    adopted_policy: AdoptedPolicySummaryV1 | None = None,
    runtime_path: Path | None = None,
) -> DirectorOutcome:
    route = resolve_director_route(env, runtime_path)
    if route.mode == "deterministic-baseline":
        digest = hashlib.sha256(
            f"baseline:{episode_id}:{':'.join(speech_ids)}".encode()
        ).hexdigest()
        return DirectorOutcome(
            mode="deterministic-baseline",
            proposal=_baseline_document(episode_id, speech_ids),
            request_hash=digest,
            served_by="deterministic-baseline-v1:no-model-involved",
            transport="deterministic-baseline",
        )
    request = director_request(
        episode_id=episode_id, source_id=source_id, total_frames=total_frames,
        rules=rules, pool=pool, speech_text=redacted_speech_text(speech_text),
        adopted_policy=adopted_policy,
    )
    return _direct_request(
        request, route, index_path=index_path, policy=policy, env=env
    )


def selection_proposal(
    episode_id: str,
    rules: EditorialRules,
    pool: CandidatePool,
    evidence_index: EvidenceIndex,
    outcome: DirectorOutcome,
) -> SelectionPlanProposal:
    producer = (
        BASELINE_PRODUCER if outcome.mode == "deterministic-baseline" else LIVE_PRODUCER
    )
    return build_selection_proposal(
        episode_id=episode_id, rules=rules, pool=pool,
        director_document=outcome.proposal.model_dump(mode="json"),
        evidence_index=evidence_index, plan_base_version="plan-base-v0",
        model_role_id=producer.model_role_id, contract_version=producer.contract_version,
        fixture_only=False,
    )


def _selection_prelude(  # noqa: PLR0913 (prelude bundles the chain's persisted inputs)
    *,
    episode_id: str,
    total_frames: int,
    analysis: RealAnalysis,
    speech: tuple[SpeechSegment, ...],
    policy_path: Path | None,
    out_dir: Path,
    editorial_grant: EpisodeEditorialGrantV1 | None,
) -> tuple[Path, EditorialRules, EvidenceIndex, tuple[str, ...]]:
    policy_file = policy_path if policy_path is not None else write_policy_snapshot(
        episode_id, out_dir, editorial_grant
    )
    rules = rules_for(total_frames, tuple(segment.segment_id for segment in speech))
    evidence_index = evidence_index_for(analysis.evidence, analysis.record.edit_source_sha256)
    speech_ids = tuple(segment.segment_id for segment in speech)
    return policy_file, rules, evidence_index, speech_ids


def select_and_reconcile(  # noqa: PLR0913 (director stage wiring over the real analysis)
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    analysis: RealAnalysis,
    pool: CandidatePool,
    speech: tuple[SpeechSegment, ...],
    policy_path: Path | None,
    out_dir: Path,
    env: dict[str, str],
    adopted_policy: AdoptedPolicySummaryV1 | None = None,
    runtime_path: Path | None = None,
    editorial_grant: EpisodeEditorialGrantV1 | None = None,
) -> tuple[DirectorOutcome, SelectionPlanProposal, ReconciliationResult, Path]:
    policy_file, rules, evidence_index, speech_ids = _selection_prelude(
        episode_id=episode_id, total_frames=total_frames, analysis=analysis,
        speech=speech, policy_path=policy_path, out_dir=out_dir,
        editorial_grant=editorial_grant,
    )
    outcome = select(
        episode_id=episode_id, source_id=source_id, total_frames=total_frames,
        rules=rules, pool=pool, speech_ids=speech_ids,
        speech_text={segment.segment_id: segment.text for segment in speech},
        index_path=analysis.record.index_path, policy=load_policy(policy_file), env=env,
        adopted_policy=adopted_policy, runtime_path=runtime_path,
    )
    selection = selection_proposal(episode_id, rules, pool, evidence_index, outcome)
    reconciled = reconcile(outcome.proposal, pool, evidence_index)
    save_selection_inputs(
        out_dir, episode_id=episode_id, source_id=source_id,
        total_frames=total_frames, analysis=analysis, pool=pool, speech=speech,
    )
    return outcome, selection, reconciled, policy_file


def _record_call(
    ledger_dir: Path | None,
    request: DirectorRequest,
    outcome: DirectorOutcome,
    *,
    attempt: int,
    triggered_by: str | None,
) -> None:
    if ledger_dir is None:
        return
    append_director_call(
        ledger_dir,
        DirectorCallEntryV1(
            episode_id=request.episode_id,
            attempt=attempt,
            request_hash=outcome.request_hash,
            served_by=outcome.served_by,
            transport=outcome.transport,
            triggered_by_refusal=triggered_by,
            created_at=now_stamp(),
        ),
    )


def _direct_once(
    source: ContradictionRetryInput,
    request: DirectorRequest,
    *,
    attempt: int,
    triggered_by: str | None,
) -> DirectorOutcome:
    route = resolve_director_route(source.env, source.runtime_path)
    if route.mode == "deterministic-baseline":
        digest = hashlib.sha256(
            f"baseline:{request.episode_id}:{':'.join(source.speech_ids)}".encode()
        ).hexdigest()
        outcome = DirectorOutcome(
            mode="deterministic-baseline",
            proposal=_baseline_document(request.episode_id, source.speech_ids),
            request_hash=digest,
            served_by="deterministic-baseline-v1:no-model-involved",
            transport="deterministic-baseline",
        )
    else:
        outcome = _direct_request(
            request, route, index_path=source.index_path,
            policy=source.policy, env=source.env,
        )
    _record_call(source.ledger_dir, request, outcome, attempt=attempt, triggered_by=triggered_by)
    return outcome


def _retry_attempt(
    attempt: int, outcome: DirectorOutcome, refusal: ValidationRefusal | None
) -> SelectionRetryAttempt:
    return SelectionRetryAttempt(
        attempt=attempt,
        request_hash=outcome.request_hash,
        served_by=outcome.served_by,
        refusal_code=refusal.code if refusal is not None else None,
        refusal_detail=refusal.detail if refusal is not None else None,
    )


def select_proposal_with_contradiction_retry(
    source: ContradictionRetryInput,
) -> ContradictionRetryResult:
    """Run the director, validate semantically, and retry ONCE on contradiction.

    Attempt 1 is the original request. When the semantic validator refuses it
    with ONLY ``keep_remove_contradiction``, attempt 2 re-invokes the
    director with the refusal appended (``refusal_feedback``); anything else
    — a clean proposal or any other refusal — returns after attempt 1 with
    no retry. A twice-refused proposal returns attempt 2 as-is: the caller
    commits (or refuses) through the commit authority, so the same typed
    refusal propagates with nothing committed and no third attempt. Each
    completed director call appends one ledger line, so both calls are
    accounted even when the retry also fails.
    """

    context = ValidationContext(
        episode=source.episode,
        edit_source=source.facts,
        capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
        locks=(),
        mandatory_candidate_ids=(),
    )
    outcome1 = _direct_once(source, source.request, attempt=1, triggered_by=None)
    selection1 = selection_proposal(
        source.request.episode_id, source.rules, source.pool,
        source.evidence_index, outcome1,
    )
    refusal1 = validate_semantic(selection1, context)
    attempt1 = _retry_attempt(1, outcome1, refusal1)
    if refusal1 is None or not is_contradiction_refusal(refusal1):
        return ContradictionRetryResult(outcome1, selection1, (attempt1,))
    reason = f"{refusal1.code}: {refusal1.detail}"
    request2 = source.request.model_copy(
        update={"refusal_feedback": refusal_feedback_text(reason)}
    )
    outcome2 = _direct_once(source, request2, attempt=2, triggered_by=reason)
    retried = replace(outcome2, attempts_made=2, first_refusal=reason)
    selection2 = selection_proposal(
        request2.episode_id, source.rules, source.pool,
        source.evidence_index, retried,
    )
    refusal2 = validate_semantic(selection2, context)
    return ContradictionRetryResult(
        retried, selection2, (attempt1, _retry_attempt(2, retried, refusal2))
    )


def select_and_reconcile_with_retry(  # noqa: PLR0913 (retry seam mirrors select_and_reconcile)
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    analysis: RealAnalysis,
    pool: CandidatePool,
    speech: tuple[SpeechSegment, ...],
    policy_path: Path | None,
    out_dir: Path,
    env: dict[str, str],
    episode: CommittedEpisodeRecord,
    facts: EditSourceFacts,
    adopted_policy: AdoptedPolicySummaryV1 | None = None,
    runtime_path: Path | None = None,
    editorial_grant: EpisodeEditorialGrantV1 | None = None,
) -> tuple[DirectorOutcome, SelectionPlanProposal, ReconciliationResult, Path]:
    """The initial-chain selection seam with the bounded contradiction retry.

    Identical inputs/outputs to :func:`select_and_reconcile`, except a
    semantically contradicting first proposal earns exactly one director
    re-invocation with the refusal fed back. The commit authority downstream
    still validates everything, so non-semantic refusals and a twice-refused
    proposal behave exactly as before (typed refusal, nothing committed).
    """

    policy_file, rules, evidence_index, speech_ids = _selection_prelude(
        episode_id=episode_id, total_frames=total_frames, analysis=analysis,
        speech=speech, policy_path=policy_path, out_dir=out_dir,
        editorial_grant=editorial_grant,
    )
    speech_text = {segment.segment_id: segment.text for segment in speech}
    request = director_request(
        episode_id=episode_id, source_id=source_id, total_frames=total_frames,
        rules=rules, pool=pool, speech_text=redacted_speech_text(speech_text),
        adopted_policy=adopted_policy,
    )
    retried = select_proposal_with_contradiction_retry(
        ContradictionRetryInput(
            request=request, index_path=analysis.record.index_path,
            policy=load_policy(policy_file), env=env, pool=pool,
            evidence_index=evidence_index, rules=rules, speech_ids=speech_ids,
            episode=episode, facts=facts, ledger_dir=out_dir,
            runtime_path=runtime_path,
        )
    )
    reconciled = reconcile(retried.outcome.proposal, pool, evidence_index)
    save_selection_inputs(
        out_dir, episode_id=episode_id, source_id=source_id,
        total_frames=total_frames, analysis=analysis, pool=pool, speech=speech,
    )
    return retried.outcome, retried.selection, reconciled, policy_file
