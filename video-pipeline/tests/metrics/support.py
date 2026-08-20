"""Synthetic event-bundle fabrication for metrics tests (pytest tmp only).

Every bundle is built from the REAL recorded-event formats the pipeline
already produces: chained operation records (``OperationRecordStore``),
hash-chained phase-0C review events (``build_event``), and hash-chained
stage-journal events. Nothing here writes outside the caller's tmp path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from services.approvals.models import OperationDraft
from services.approvals.store import OperationRecordStore
from services.contracts.primitives import Producer
from services.job_runner.stage_runner_models import (
    GENESIS_HASH,
    JournalEventKind,
    RunnerIdentity,
    StageJournalEvent,
    hash_event,
)
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    EventKind0C,
    build_event,
)
from services.review_command.models import (
    ApproveEditorialPlanProposal0C,
    CandidateTarget0C,
    Confidence0C,
    ProposalAmbiguity0C,
    RemoveSegmentProposal0C,
)

RUNNER_IDENTITY = RunnerIdentity(
    runner_version="test-runner-v1",
    code_snapshot_id="snapshot-1",
    producer=Producer(name="metrics-test", version="1"),
)


def _target_hash(name: str) -> str:
    return hashlib.sha256(f"metrics-target-{name}".encode()).hexdigest()


class BundleBuilder:
    """Assemble a metrics event bundle directory under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._episodes: list[dict[str, object]] = []
        self._timing: list[dict[str, object]] = []
        self._review: list[tuple[str, BaseModel]] = []
        self._stage: list[tuple[str, BaseModel]] = []
        self._qc: list[dict[str, object]] = []
        self._inventory: list[dict[str, object]] = []
        self._sources: list[dict[str, object]] = []
        self._decisions: list[dict[str, object]] = []
        self._items: list[dict[str, object]] = []
        self._claims: list[dict[str, object]] = []
        self._review_seq = 0
        self._review_prev: str = GENESIS_EVENT_HASH
        self._stage_seq = 0
        self._stage_prev: str = GENESIS_HASH
        self._proposal_seq = 0
        self._op_target_seq = 0
        self._op_store: OperationRecordStore | None = None

    def episode(
        self,
        episode_id: str,
        *,
        kind: str = "real",
        in_contract: bool = True,
        exclusion_reason: str | None = None,
        approval_record_id: str | None = None,
    ) -> None:
        self._episodes.append(
            {
                "episode_id": episode_id,
                "episode_kind": kind,
                "in_contract": in_contract,
                "exclusion_reason": exclusion_reason,
                "editorial_approval_record_id": approval_record_id,
            }
        )

    def timing(
        self,
        episode_id: str,
        phase: str,
        marker: str,
        timestamp: int | None,
        *,
        source: str = "recorded",
    ) -> None:
        self._timing.append(
            {
                "episode_id": episode_id,
                "phase": phase,
                "marker": marker,
                "timestamp_unix": timestamp,
                "source": source,
            }
        )

    def phase_timing(self, episode_id: str, phase: str, start: int, end: int) -> None:
        self.timing(episode_id, phase, "start", start)
        self.timing(episode_id, phase, "end", end)

    def review(  # noqa: PLR0913 (explicit event shape; kwargs are the contract)
        self,
        episode_id: str,
        *,
        kind: str = "decision_applied",
        decision_id: str | None = None,
        base: str = "v1",
        result: str | None = None,
        reason: str | None = None,
        approve_proposal: bool = False,
    ) -> None:
        self._proposal_seq += 1
        proposal_id = f"prop-{self._proposal_seq:04d}"
        confidence = Confidence0C(num=1, den=1)
        ambiguity = ProposalAmbiguity0C(status="clear")
        if approve_proposal:
            proposal = ApproveEditorialPlanProposal0C(
                command_kind="approve_editorial_plan",
                actor_intent="operator",
                proposal_id=proposal_id,
                base_plan_version=base,
                sequence=self._proposal_seq,
                confidence=confidence,
                ambiguity=ambiguity,
            )
        else:
            proposal = RemoveSegmentProposal0C(
                command_kind="remove_segment",
                actor_intent="model",
                proposal_id=proposal_id,
                base_plan_version=base,
                sequence=self._proposal_seq,
                confidence=confidence,
                ambiguity=ambiguity,
                candidate_targets=(
                    CandidateTarget0C(item_id="item-1", evidence="transcript marker"),
                ),
            )
        event_kind: EventKind0C = kind  # type: ignore[assignment]
        applied = kind == "decision_applied"
        if applied and decision_id is None:
            decision_id = f"opr-decision-{self._proposal_seq:04d}"
        event = build_event(
            sequence=self._review_seq + 1,
            kind=event_kind,
            proposal=proposal,
            base_plan_version=base,
            previous_event_hash=self._review_prev,
            actor_intent="operator" if applied else "model",
            applied=applied,
            result_plan_version=result if applied else None,
            decision_id=decision_id if applied else None,
            reason=reason,
        )
        self._review_seq += 1
        self._review_prev = event.event_id
        self._review.append((episode_id, event))

    def stage(
        self,
        episode_id: str,
        stage_name: str,
        journal_kind: JournalEventKind,
        *,
        attempt: int = 1,
        idempotency_key: str | None = None,
    ) -> None:
        self._stage_seq += 1
        key = idempotency_key or hashlib.sha256(
            f"{episode_id}:{stage_name}".encode()
        ).hexdigest()
        event = StageJournalEvent(
            sequence=self._stage_seq,
            previous_event_hash=self._stage_prev,
            event_hash=GENESIS_HASH,
            job_id=f"job-{episode_id}",
            stage_name=stage_name,
            idempotency_key=key,
            kind=journal_kind,
            attempt=attempt,
            identity=RUNNER_IDENTITY,
        )
        self._stage_prev = hash_event(event)
        self._stage.append((episode_id, event.model_copy(update={"event_hash": self._stage_prev})))

    def qc_outcome(
        self,
        episode_id: str,
        *,
        verdict: str = "passed",
        blockers: int = 0,
        major: int = 0,
        minor: int = 0,
    ) -> None:
        self._qc.append(
            {
                "episode_id": episode_id,
                "verdict": verdict,
                "blocker_count": blockers,
                "major_count": major,
                "minor_count": minor,
            }
        )

    def artifact(
        self,
        episode_id: str,
        artifact_id: str,
        retention_class: str,
        byte_size: int,
    ) -> None:
        self._inventory.append(
            {
                "episode_id": episode_id,
                "artifact_id": artifact_id,
                "retention_class": retention_class,
                "byte_size": byte_size,
            }
        )

    def trace_source(self, episode_id: str, source_id: str) -> None:
        self._sources.append({"episode_id": episode_id, "source_id": source_id})

    def trace_decision(
        self, episode_id: str, decision_id: str, source_span_ids: tuple[str, ...]
    ) -> None:
        self._decisions.append(
            {
                "episode_id": episode_id,
                "decision_id": decision_id,
                "source_span_ids": list(source_span_ids),
            }
        )

    def trace_item(self, episode_id: str, item_id: str, decision_id: str) -> None:
        self._items.append(
            {
                "episode_id": episode_id,
                "item_id": item_id,
                "decision_id": decision_id,
            }
        )

    def claim(self, claim_id: str, metric: str, value: int) -> None:
        self._claims.append({"claim_id": claim_id, "metric": metric, "value": value})

    def approval(  # noqa: PLR0913 (explicit record shape; kwargs are the contract)
        self,
        *,
        purpose: str = "editorial",
        decision: str = "approve",
        runner_class: str = "operator",
        fixture_only: bool = False,
        uid: int | None = 501,
        tty: str | None = "/dev/ttys001",
        wall_time: int = 1700000000,
    ) -> str:
        if self._op_store is None:
            self._op_store = OperationRecordStore(self.root / "operation-records.jsonl")
        self._op_target_seq += 1
        target_type = {
            "editorial": "edit-plan",
            "presentation": "presentation-bundle",
            "privacy": "privacy-report",
            "rights": "rights-report",
            "final": "final-render",
            "publication": "publication-bundle",
            "manual_freeze": "frozen-timeline",
        }[purpose]
        record = self._op_store.append(
            OperationDraft.model_validate(
                {
                    "purpose": purpose,
                    "target_type": target_type,
                    "target_hash": _target_hash(f"{self._op_target_seq:04d}"),
                    "decision": decision,
                    "actor_id": "op-operator",
                    "uid": uid,
                    "tty": tty,
                    "wall_time_unix": wall_time,
                    "fixture_only": fixture_only,
                    "runner_class": runner_class,
                }
            )
        )
        return record.record_id

    def write(self) -> Path:
        episodes = {
            "schema_version": "metrics-episodes-v1",
            "episodes": self._episodes,
        }
        (self.root / "episodes.json").write_bytes(canonical_json(episodes))
        _write_lines(self.root / "timing-events.jsonl", self._timing)
        _write_wrapped(self.root / "review-events.jsonl", self._review)
        _write_wrapped(self.root / "stage-events.jsonl", self._stage)
        _write_lines(self.root / "qc-outcomes.jsonl", self._qc)
        _write_lines(self.root / "artifact-inventory.jsonl", self._inventory)
        _write_lines(self.root / "trace-sources.jsonl", self._sources)
        _write_lines(self.root / "trace-decisions.jsonl", self._decisions)
        _write_lines(self.root / "trace-build-items.jsonl", self._items)
        if self._claims:
            (self.root / "claims.json").write_bytes(
                canonical_json(
                    {"schema_version": "metrics-claims-v1", "claims": self._claims}
                )
            )
        return self.root


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _write_lines(path: Path, rows: list[dict[str, object]]) -> None:
    if rows:
        path.write_bytes(b"".join(canonical_json(row) + b"\n" for row in rows))


def _write_wrapped(path: Path, rows: Sequence[tuple[str, BaseModel]]) -> None:
    if rows:
        path.write_bytes(
            b"".join(
                canonical_json(
                    {"episode_id": episode_id, "event": event.model_dump(mode="json")}
                )
                + b"\n"
                for episode_id, event in rows
            )
        )


def real_approved_episode(builder: BundleBuilder, episode_id: str) -> str:
    record_id = builder.approval()
    builder.episode(episode_id, approval_record_id=record_id)
    return record_id


def full_phase_timing(  # noqa: PLR0913 (explicit phase durations; kwargs are the contract)
    builder: BundleBuilder,
    episode_id: str,
    *,
    base: int = 1700000000,
    session_s: int = 60,
    review_s: int = 120,
    correction_s: int = 30,
    qc_s: int = 45,
    final_s: int = 15,
) -> None:
    """Write five complete phase intervals back-to-back from ``base``."""

    cursor = base
    for phase, seconds in (
        ("session", session_s),
        ("review", review_s),
        ("correction", correction_s),
        ("qc", qc_s),
        ("final", final_s),
    ):
        builder.phase_timing(episode_id, phase, cursor, cursor + seconds)
        cursor += seconds
