"""The real-episode Editorial Director: two honestly-labeled modes (Todo 46).

DETERMINISTIC BASELINE (no credentials): keep-all-speech — every speech segment
selected, must-include all, no removals — assembled through the Todo-40
candidate machinery with producer ``deterministic-baseline-v1`` and an
explicit no-model-involved marker; zero model calls, zero network. LIVE
(``EDITORIAL_DIRECTOR_API_KEY`` set): the Todo-39 adapter over the stdlib
urllib transport, bounded evidence via the Todo-37 MediaQueryApi over the
REAL index, transcript transport authorized by the Todo-12 gate over the
resolved production policy (deny-by-default); a live refusal is honest
failure — the chain NEVER silently falls back.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from services.cli.live_editorial import LiveHttpTransport
from services.cli.real_policy import load_policy, write_policy_snapshot
from services.cli.real_pool import evidence_index_for, rules_for
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
from services.editorial.models import DeclaredCandidate, DirectorRequest
from services.editorial.pin import load_pin
from services.editorial.policy import decide_production_transport_policy
from services.editorial.proposal_builder import build_selection_proposal
from services.editorial.reconcile import reconcile
from services.editorial.transport import CREDENTIALS_ENV
from services.fixtures.manifest_phase1 import EditorialRules, EditSourceSpec
from services.media_query.api import MediaQueryApi

if TYPE_CHECKING:
    from services.cli.real_analyze import RealAnalysis
    from services.cli.real_pool import SpeechSegment
    from services.config.models import ResolvedConfig
    from services.editorial.reconcile import ReconciliationResult

BASELINE_PRODUCER = ProposalProducer(
    model_role_id="deterministic-baseline-v1",
    contract_version="deterministic-baseline-v1:no-model-involved",
)
LIVE_PRODUCER = ProposalProducer(
    model_role_id="editorial-director",
    contract_version="phase-1-editorial-director-v1",
)
NEUTRAL_SCORE = 5


class RealDirectorError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


type DirectorMode = Literal["deterministic-baseline", "live"]


@dataclass(frozen=True, slots=True)
class DirectorOutcome:
    mode: DirectorMode
    proposal: EditorialSelectionProposal
    request_hash: str
    served_by: str


def director_mode(env: dict[str, str]) -> DirectorMode:
    return "live" if env.get(CREDENTIALS_ENV) else "deterministic-baseline"


def director_request(  # noqa: PLR0913 (declared-candidate table from the real pool)
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    rules: EditorialRules,
    pool: CandidatePool,
    speech_text: dict[str, str],
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


def _live(
    request: DirectorRequest, index_path: str, policy: ResolvedConfig, env: dict[str, str]
) -> DirectorOutcome:
    pin = load_pin()
    transport = LiveHttpTransport(request=request, pin=pin, env=env)
    director = EditorialDirector(
        transport=transport,
        pin=pin,
        policy_decider=lambda episode: decide_production_transport_policy(episode, policy),
        transport_kind="live-http",
    )
    with MediaQueryApi.open(Path(index_path)) as api:
        result = director.run(request, api=api)
    if result.error is not None:
        raise RealDirectorError(
            "director_failed",
            f"{result.envelope.status}: {result.error.detail} "
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
        served_by=result.metadata.observed_model or f"live:{pin.model_id}",
    )


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
) -> DirectorOutcome:
    if director_mode(env) == "deterministic-baseline":
        digest = hashlib.sha256(
            f"baseline:{episode_id}:{':'.join(speech_ids)}".encode()
        ).hexdigest()
        return DirectorOutcome(
            mode="deterministic-baseline",
            proposal=_baseline_document(episode_id, speech_ids),
            request_hash=digest,
            served_by="deterministic-baseline-v1:no-model-involved",
        )
    request = director_request(
        episode_id=episode_id, source_id=source_id, total_frames=total_frames,
        rules=rules, pool=pool, speech_text=speech_text,
    )
    return _live(request, index_path, policy, env)


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
) -> tuple[DirectorOutcome, SelectionPlanProposal, ReconciliationResult, Path]:
    policy_file = policy_path if policy_path is not None else write_policy_snapshot(
        episode_id, out_dir
    )
    rules = rules_for(total_frames, tuple(segment.segment_id for segment in speech))
    evidence_index = evidence_index_for(analysis.evidence, analysis.record.edit_source_sha256)
    speech_ids = tuple(segment.segment_id for segment in speech)
    outcome = select(
        episode_id=episode_id, source_id=source_id, total_frames=total_frames,
        rules=rules, pool=pool, speech_ids=speech_ids,
        speech_text={segment.segment_id: segment.text for segment in speech},
        index_path=analysis.record.index_path, policy=load_policy(policy_file), env=env,
    )
    selection = selection_proposal(episode_id, rules, pool, evidence_index, outcome)
    reconciled = reconcile(outcome.proposal, pool, evidence_index)
    return outcome, selection, reconciled, policy_file
