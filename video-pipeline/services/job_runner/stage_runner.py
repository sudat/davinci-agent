"""Idempotent Stage Runner orchestration (Todo 11, PRD 6 / 31.5).

One ``StageRunKey`` (stage + sorted input hashes + runner version +
code snapshot) commits exactly ONE output: a succeeded run is reused
byte-verified from the store, a crash between publish and record
re-adopts the content-addressed artifact on restart, and a crash
before publish is a clean re-run. Failures classify deterministically;
attempts journal to the hash-chained JSONL, record via the Todo-9
idempotent upsert, and adopt through the Todo-10 StateLane CAS under
the named lease ``stage:<job_id>:<stage_name>``. No product AI runs
retries (PRD Appendix C 9).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.artifact_registry.store_view import (
    StoreViewError,
    meta_path,
    parse_meta_bytes,
    read_file_nofollow,
)
from services.artifact_store.models import PublicationIntent
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from services.contracts.primitives import ArtifactEnvelope, ArtifactId, Identifier, Sha256
from services.job_runner.cas import current_job_state
from services.job_runner.lanes import StateLane
from services.job_runner.stage_classify import classify_error
from services.job_runner.stage_runner_models import (
    FailureClass,
    JournalEventKind,
    LogicalClock,
    RetryPolicy,
    RunnerIdentity,
    StageFnError,
    StageRunJournal,
    StageRunKey,
    StageRunnerError,
    StageRunRecord,
    StageRunResult,
)
from services.job_runner.state_models import StageRunRow

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from services.artifact_registry.registry import ArtifactRegistry
    from services.contracts.primitives import Producer
    from services.job_runner.state_models import JobStatus
    from services.job_runner.state_store import StateStore

    RunnerFn = Callable[[int], bytes]

STAGE_LEASE_TTL_SECONDS: Final = 3600
STAGE_OUTPUT_TYPE: Final = "stage-output"
STAGE_OUTPUT_SCHEMA: Final = "stage-run-v1"


def stage_resource(job_id: str, stage_name: str) -> Identifier:
    """Named lease resource serializing one stage of one job."""
    return f"stage:{job_id}:{stage_name}"


def stage_artifact_id(job_id: str, stage_name: str, idempotency_key: str) -> ArtifactId:
    """Deterministic output artifact id (the crash-recovery lookup anchor)."""
    return f"stage-output:{job_id}:{stage_name}:{idempotency_key[:16]}"


@dataclass(frozen=True, slots=True)
class _RunContext:
    store: StateStore
    registry: ArtifactRegistry
    job_id: Identifier
    key: StageRunKey
    journal: StageRunJournal
    clock: LogicalClock
    started_seq: int
    started_status: JobStatus
    started_adopted_hash: str | None


class StageRunner:
    """Runs one stage of one job idempotently under a named stage lease."""

    def __init__(  # noqa: PLR0913 (identity + authority wiring is the constructor)
        self,
        *,
        artifact_store: ArtifactStore,
        journal_root: Path,
        runner_version: Identifier,
        code_snapshot_id: Identifier,
        producer: Producer,
        holder: Identifier = "stage-runner",
        lease_ttl_seconds: int = STAGE_LEASE_TTL_SECONDS,
        before_record_hook: Callable[[str], None] | None = None,  # crash-sim seam
    ) -> None:
        self._artifact_store = artifact_store
        self._journal_root = journal_root
        self._identity = RunnerIdentity(
            runner_version=runner_version, code_snapshot_id=code_snapshot_id, producer=producer
        )
        self._holder = holder
        self._lease_ttl = lease_ttl_seconds
        self._before_record_hook = before_record_hook

    def run(  # noqa: PLR0913, PLR0917 (Todo-11 contract; the signature is fixed)
        self, store: StateStore, registry: ArtifactRegistry, job_id: Identifier,
        stage_name: Identifier, input_hashes: tuple[Sha256, ...], runner_fn: RunnerFn,
        policy: RetryPolicy, clock: LogicalClock,
    ) -> StageRunResult:
        key = StageRunKey(
            stage_name=stage_name, input_hashes=input_hashes,
            runner_version=self._identity.runner_version,
            code_snapshot_id=self._identity.code_snapshot_id,
        )
        starting = current_job_state(store, job_id)
        context = _RunContext(
            store=store, registry=registry, job_id=job_id, key=key,
            journal=StageRunJournal(self._journal_root, job_id), clock=clock,
            started_seq=starting.updated_at_seq,
            started_status=starting.status,
            started_adopted_hash=starting.adopted_artifact_hash,
        )
        resource = stage_resource(job_id, stage_name)
        store.acquire_lease(
            resource=resource, holder=self._holder, now=clock.now, ttl_seconds=self._lease_ttl
        )
        try:
            succeeded = self._matching_row(context, require_succeeded=True)
            if succeeded is not None:
                return self._reuse(context, succeeded)
            recovered = self._published_hash(
                registry, stage_artifact_id(job_id, stage_name, key.idempotency_key)
            )
            if recovered is not None:
                payload, _ref = self._artifact_store.reopen(recovered)
                return self._commit(context, payload, attempt=0, kind="run-recovered")
            return self._attempts(context, runner_fn, policy)
        finally:
            store.release_lease(resource=resource, holder=self._holder, now=clock.now)

    def _attempts(
        self, context: _RunContext, runner_fn: RunnerFn, policy: RetryPolicy
    ) -> StageRunResult:
        store, key, journal = context.store, context.key, context.journal
        for attempt in range(1, policy.max_attempts + 1):
            if attempt == 1:
                store.record_stage_run(StageRunRow(
                    job_id=context.job_id, stage_name=key.stage_name,
                    input_artifact_hashes=key.input_hashes, adopted_artifact_hash=None,
                    status="running", idempotency_key=key.idempotency_key,
                ))
            try:
                payload = runner_fn(attempt)
            except StageFnError as error:
                failure_class = classify_error(error.code)
                store.bump_retry(job_id=context.job_id, idempotency_key=key.idempotency_key,
                                 error_code=error.code)
                store.set_stage_status(job_id=context.job_id, idempotency_key=key.idempotency_key,
                                       status="failed_retryable", last_error_code=error.code)
                journal.append(journal.mint(
                    job_id=context.job_id, key=key, identity=self._identity,
                    kind="attempt-failed", attempt=attempt, failure_class=failure_class,
                    error_code=error.code,
                ))
                if failure_class == "transient" and attempt < policy.max_attempts:
                    context.clock.advance(policy.retry_delay(attempt))
                    continue
                return self._block(context, attempt, failure_class, error.code)
            return self._commit(context, payload, attempt=attempt, kind="attempt-succeeded")
        raise StageRunnerError("retry-overflow", "attempt loop ended without classification")

    def _commit(
        self, context: _RunContext, payload: bytes, *, attempt: int, kind: JournalEventKind
    ) -> StageRunResult:
        store, key, journal, clock = (
            context.store, context.key, context.journal, context.clock,
        )
        content_hash = hashlib.sha256(payload).hexdigest()
        envelope = ArtifactEnvelope(
            artifact_id=stage_artifact_id(context.job_id, key.stage_name, key.idempotency_key),
            artifact_type=STAGE_OUTPUT_TYPE, schema_version=STAGE_OUTPUT_SCHEMA,
            content_hash=content_hash, producer=self._identity.producer, inputs=(),
        )
        receipt = self._artifact_store.publish(PublicationIntent(envelope=envelope), payload)
        context.registry.register(self._artifact_store, receipt)
        if self._before_record_hook is not None:
            self._before_record_hook(content_hash)
        existing = self._matching_row(context, require_succeeded=False)
        store.record_stage_run(StageRunRow(
            job_id=context.job_id, stage_name=key.stage_name,
            input_artifact_hashes=key.input_hashes, adopted_artifact_hash=content_hash,
            status="succeeded", idempotency_key=key.idempotency_key,
            retry_count=0 if existing is None else existing.retry_count,
        ))
        lane = StateLane(store, context.job_id, holder=self._holder)
        lane.acquire(now=clock.now, ttl_seconds=self._lease_ttl)
        try:
            current = current_job_state(store, context.job_id)
            drifted = (
                current.status != context.started_status
                or current.adopted_artifact_hash != context.started_adopted_hash
            )
            if drifted:
                return self._superseded(context, payload_hash=content_hash, attempt=attempt)
            if current.adopted_artifact_hash != content_hash:
                lane.apply(
                    expected_status=current.status,
                    expected_parent_hash=current.adopted_artifact_hash,
                    new_status=current.status, new_artifact_hash=content_hash, now=clock.now,
                )
        finally:
            lane.release(now=clock.now)
        journal.append(journal.mint(
            job_id=context.job_id, key=key, identity=self._identity, kind=kind,
            attempt=attempt, output_artifact_hash=content_hash,
        ))
        record = StageRunRecord(
            key=key, attempt=attempt, output_artifact_hash=content_hash,
            started_seq=context.started_seq,
            ended_seq=current_job_state(store, context.job_id).updated_at_seq,
            identity=self._identity,
        )
        outcome = "succeeded" if kind == "attempt-succeeded" else "recovered"
        return StageRunResult(
            outcome=outcome, output_artifact_hash=content_hash, attempts=attempt, record=record
        )

    def _superseded(
        self, context: _RunContext, *, payload_hash: str, attempt: int
    ) -> StageRunResult:
        """Stale output: the parent moved on mid-run; adopt NOTHING."""
        context.journal.append(context.journal.mint(
            job_id=context.job_id, key=context.key, identity=self._identity,
            kind="run-superseded", attempt=attempt, output_artifact_hash=payload_hash,
        ))
        record = StageRunRecord(
            key=context.key, attempt=attempt, output_artifact_hash=payload_hash,
            started_seq=context.started_seq,
            ended_seq=current_job_state(context.store, context.job_id).updated_at_seq,
            identity=self._identity,
        )
        return StageRunResult(
            outcome="superseded", output_artifact_hash=payload_hash,
            attempts=attempt, record=record,
        )

    def _block(
        self, context: _RunContext, attempt: int, failure_class: FailureClass,
        error_code: Identifier,
    ) -> StageRunResult:
        context.store.set_stage_status(
            job_id=context.job_id, idempotency_key=context.key.idempotency_key,
            status="failed_blocked", last_error_code=error_code,
        )
        context.journal.append(context.journal.mint(
            job_id=context.job_id, key=context.key, identity=self._identity,
            kind="run-blocked", attempt=attempt, failure_class=failure_class,
            error_code=error_code,
        ))
        record = StageRunRecord(
            key=context.key, attempt=attempt, failure_class=failure_class,
            error_code=error_code, started_seq=context.started_seq,
            ended_seq=current_job_state(context.store, context.job_id).updated_at_seq,
            identity=self._identity,
        )
        return StageRunResult(
            outcome="failed_blocked", attempts=attempt, failure_class=failure_class,
            error_code=error_code, needs_human=failure_class == "blocking_human",
            record=record,
        )

    def _reuse(self, context: _RunContext, row: StageRunRow) -> StageRunResult:
        recorded = row.adopted_artifact_hash
        if recorded is None:
            raise StageRunnerError(
                "reuse-missing-hash", f"succeeded run {row.idempotency_key} has no output hash"
            )
        try:
            self._artifact_store.reopen(recorded)  # verify bytes against the hash
        except StoreRefusalError as error:
            raise StageRunnerError(
                "reuse-verify-failed",
                f"recorded output {recorded} failed store verification: {error.code}",
            ) from error
        context.journal.append(context.journal.mint(
            job_id=context.job_id, key=context.key, identity=self._identity,
            kind="run-reused", attempt=0, output_artifact_hash=recorded,
        ))
        record = StageRunRecord(
            key=context.key, attempt=0, output_artifact_hash=recorded,
            started_seq=context.started_seq, ended_seq=context.started_seq,
            identity=self._identity,
        )
        return StageRunResult(
            outcome="reused", output_artifact_hash=recorded, attempts=0, record=record
        )

    def _matching_row(
        self, context: _RunContext, *, require_succeeded: bool
    ) -> StageRunRow | None:
        for row in context.store.get_job_snapshot(context.job_id).stage_runs:
            if row.idempotency_key != context.key.idempotency_key:
                continue
            if not require_succeeded or (
                row.status == "succeeded" and row.adopted_artifact_hash is not None
            ):
                return row
        return None

    def _published_hash(
        self, registry: ArtifactRegistry, artifact_id: ArtifactId
    ) -> Sha256 | None:
        entry = registry.load().entries.get(artifact_id)
        if entry is not None:
            return entry.content_sha256
        raw = read_file_nofollow(meta_path(self._artifact_store.store_root, artifact_id))
        if raw is None:
            return None
        try:
            intent = parse_meta_bytes(raw, expected_id=artifact_id)
        except StoreViewError as error:
            raise StageRunnerError(
                "recovery-meta-invalid",
                f"published artifact meta for {artifact_id} is unreadable: {error.code}",
            ) from error
        return intent.envelope.content_hash


__all__ = ["StageRunner", "StageRunnerError", "stage_artifact_id", "stage_resource"]
