"""Bounded contradiction retry for the r8 keep_remove_contradiction blocker.

Hermetic by construction: a REAL MediaQueryApi over a manifest-derived
synthetic index, an allowing production policy snapshot, and a fake codex
runner (the real ``codex`` binary is never touched). The pool mirrors the
r8 measured shape — a speech segment and a false_start sharing one identical
span — so a keep/drop split across them deterministically trips the
semantic validator, exactly as the live run did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli import live_editorial_codex
from services.cli.director_call_ledger import load_director_calls
from services.cli.real_director import (
    ContradictionRetryInput,
    is_contradiction_refusal,
    refusal_feedback_text,
    select_proposal_with_contradiction_retry,
)
from services.contracts.editorial_model import EditorialSelectionProposal, SelectionEntry
from services.contracts.primitives import ArtifactRef
from services.editorial.candidate_models import (
    AnalyzerSegmentRecord,
    CandidatePool,
    CandidateProvenance,
    CandidateSpan,
    EvidenceIndex,
)
from services.episode_cockpit.models import EpisodeEditorialGrantV1
from services.foundation_io import canonical_model_bytes
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.job_runner.state_store import StateStore
from services.validate.selection_commit import SelectionCommitAuthority
from services.validate.selection_models import (
    CommittedEpisodeRecord,
    EditSourceFacts,
    SelectionCommitOutcome,
    SelectionCommitRefusal,
    ValidationContext,
    ValidationRefusal,
)
from services.validate.selection_plan_store import initialize_plan_store, load_index
from tests.editorial.support import build_index, load_manifest
from tests.validate.support import JOB, advance_to_plan_proposed

EPISODE_ID = "ep-contradiction-retry"
SOURCE_ID = "src-retry-test"
POOL_SHA = "cd" * 32
RUNTIME_ENV = "EDITORIAL_RUNTIME_CONFIG"
BASE = "plan-base-v0"
EVIDENCE = (ArtifactRef(artifact_id="art-retry-1", sha256="ab" * 32),)


@dataclass
class FakeCodexRunner:
    """Recording stand-in for the codex-exec call (never a subprocess)."""

    replies: list[str] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "model": model, "images": images, "timeout_s": timeout_s}
        )
        return self.replies.pop(0)


def _stub_codex(monkeypatch: pytest.MonkeyPatch, fake: FakeCodexRunner) -> FakeCodexRunner:
    monkeypatch.setattr(live_editorial_codex, "make_codex_runner", lambda *a: fake)
    return fake


def _span(start: int, end: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start,
        end_frame=end,
        rate_num=30,
        rate_den=1,
        start_ms=start * 1000 // 30,
        end_ms=end * 1000 // 30,
    )


def _seam(tmp_path: Path) -> tuple[CandidatePool, EvidenceIndex, dict[str, str], str]:
    manifest = load_manifest("p1-ref-01-clean-ja")
    episode = build_index(tmp_path, manifest)
    voiced = [s for s in manifest.transcript.segments if s.text.strip()]
    first, second = voiced[0], voiced[1]
    pool = CandidatePool(
        source_id=SOURCE_ID,
        edit_source_sha=POOL_SHA,
        total_frames=manifest.edit_source.total_frames,
        segments=(
            AnalyzerSegmentRecord(
                segment_id="s1",
                kind="speech",
                span=_span(first.span.start_frame, first.span.end_frame),
                evidence=EVIDENCE,
                provenance=CandidateProvenance(
                    analyzer_version="todo34-v1", rule_ids=("p1-test-v1",)
                ),
            ),
            AnalyzerSegmentRecord(
                segment_id="s2",
                kind="speech",
                span=_span(second.span.start_frame, second.span.end_frame),
                evidence=EVIDENCE,
                provenance=CandidateProvenance(
                    analyzer_version="todo34-v1", rule_ids=("p1-test-v1",)
                ),
            ),
            AnalyzerSegmentRecord(
                segment_id="fa1",
                kind="false_start",
                span=_span(first.span.start_frame, first.span.end_frame),
                evidence=EVIDENCE,
                provenance=CandidateProvenance(
                    analyzer_version="todo34-v1", rule_ids=("p1-test-v1",)
                ),
            ),
        ),
    )
    return (
        pool,
        EvidenceIndex(rows=EVIDENCE, edit_source_sha=POOL_SHA),
        {"s1": first.text, "s2": second.text},
        str(episode.db_path),
    )


def _proposal(entries: tuple[tuple[str, str], ...]) -> EditorialSelectionProposal:
    return EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id="sel-retry-test-v1",
        episode_id=EPISODE_ID,
        actor_intent="model",
        selection=tuple(
            SelectionEntry(segment_id=sid, action=action, reason_code="retry-test")  # type: ignore[arg-type]
            for sid, action in entries
        ),
        confidence=(len(entries), len(entries)),
    )


CONTRADICTING = (
    ("s1", "selected"),
    ("s2", "selected"),
    ("fa1", "dropped"),
)
CLEAN = (
    ("s1", "selected"),
    ("s2", "selected"),
    ("fa1", "selected"),
)


def _input_for(
    tmp_path: Path, env: dict[str, str], facts_sha: str = POOL_SHA
) -> ContradictionRetryInput:
    from services.cli.real_director import director_request  # noqa: PLC0415 (seam helper)
    from services.cli.real_policy import policy_for_grant  # noqa: PLC0415 (seam helper)

    manifest = load_manifest("p1-ref-01-clean-ja")
    pool, evidence_index, speech_text, index_path = _seam(tmp_path)
    grant = EpisodeEditorialGrantV1(
        episode_id=EPISODE_ID,
        granted=True,
        data_class="transcript",
        stage="editorial_direct",
        granted_at="2026-09-09T00:00:00+00:00",
        note="contradiction retry test",
    )
    return ContradictionRetryInput(
        request=director_request(
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            total_frames=manifest.edit_source.total_frames,
            rules=manifest.editorial_rules,
            pool=pool,
            speech_text=speech_text,
        ),
        index_path=index_path,
        policy=policy_for_grant(EPISODE_ID, grant),
        env=env,
        pool=pool,
        evidence_index=evidence_index,
        rules=manifest.editorial_rules,
        speech_ids=("s1", "s2"),
        episode=CommittedEpisodeRecord(
            episode_id=EPISODE_ID,
            contract_id="talking-head-mvp-v1",
            status="supported",
            fixture_only=False,
        ),
        facts=EditSourceFacts(
            source_id=SOURCE_ID,
            edit_source_sha=facts_sha,
            total_frames=manifest.edit_source.total_frames,
        ),
        ledger_dir=tmp_path / "run",
    )


def _runtime(tmp_path: Path) -> Path:
    path = tmp_path / "codex.json"
    path.write_text(
        json.dumps({"mode": "production_model", "transport": "codex-exec"}),
        encoding="utf-8",
    )
    return path


def _commit(source: ContradictionRetryInput, tmp_path: Path, selection: object):
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id=EPISODE_ID, current_stage="editorial")
    advance_to_plan_proposed(state)
    plan_dir = tmp_path / "selection-plan"
    initialize_plan_store(plan_dir, episode_id=EPISODE_ID, base_version=BASE)
    authority = SelectionCommitAuthority(
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        registry=ArtifactRegistry(tmp_path / "registry"),
        state_store=state,
        job_id=JOB,
        plan_dir=plan_dir,
        context=ValidationContext(
            episode=source.episode,
            edit_source=source.facts,
            capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
            locks=(),
        ),
        holder="retry-test",
    )
    return authority.commit(selection, now=100, ttl_seconds=100), plan_dir


def test_retry_recovers_contradiction_and_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {RUNTIME_ENV: str(_runtime(tmp_path))}
    source = _input_for(tmp_path, env)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(
            replies=[
                canonical_model_bytes(_proposal(CONTRADICTING)).decode("utf-8"),
                canonical_model_bytes(_proposal(CLEAN)).decode("utf-8"),
            ]
        ),
    )

    result = select_proposal_with_contradiction_retry(source)

    assert len(fake.calls) == 2
    first_prompt = str(fake.calls[0]["prompt"])
    second_prompt = str(fake.calls[1]["prompt"])
    assert "確定を拒否" not in first_prompt
    assert len(result.attempts) == 2
    assert result.attempts[0].refusal_code == "keep_remove_contradiction"
    assert result.attempts[0].refusal_detail is not None
    assert result.attempts[1].refusal_code is None
    expected_feedback = refusal_feedback_text(
        f"keep_remove_contradiction: {result.attempts[0].refusal_detail}"
    )
    assert expected_feedback in second_prompt
    assert result.outcome.attempts_made == 2
    assert result.outcome.first_refusal == (
        f"keep_remove_contradiction: {result.attempts[0].refusal_detail}"
    )
    calls = load_director_calls(tmp_path / "run")
    assert [entry.attempt for entry in calls] == [1, 2]
    assert calls[1].triggered_by_refusal == result.outcome.first_refusal

    outcome, plan_dir = _commit(
        source, tmp_path, result.selection.model_dump(mode="json")
    )
    assert isinstance(outcome, SelectionCommitOutcome)
    assert outcome.version == 1
    assert load_index(plan_dir).versions != {}


def test_double_contradiction_propagates_refusal_with_nothing_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {RUNTIME_ENV: str(_runtime(tmp_path))}
    source = _input_for(tmp_path, env)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(
            replies=[
                canonical_model_bytes(_proposal(CONTRADICTING)).decode("utf-8"),
                canonical_model_bytes(_proposal(CONTRADICTING)).decode("utf-8"),
            ]
        ),
    )

    result = select_proposal_with_contradiction_retry(source)

    assert len(fake.calls) == 2
    assert [attempt.refusal_code for attempt in result.attempts] == [
        "keep_remove_contradiction",
        "keep_remove_contradiction",
    ]
    assert result.outcome.attempts_made == 2
    assert result.outcome.first_refusal is not None
    assert len(load_director_calls(tmp_path / "run")) == 2

    outcome, plan_dir = _commit(
        source, tmp_path, result.selection.model_dump(mode="json")
    )
    assert isinstance(outcome, SelectionCommitRefusal)
    assert outcome.validator == "semantic"
    assert outcome.code == "keep_remove_contradiction"
    assert load_index(plan_dir).versions == {}


def test_clean_first_proposal_commits_with_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {RUNTIME_ENV: str(_runtime(tmp_path))}
    source = _input_for(tmp_path, env)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(
            replies=[canonical_model_bytes(_proposal(CLEAN)).decode("utf-8")]
        ),
    )

    result = select_proposal_with_contradiction_retry(source)

    assert len(fake.calls) == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].refusal_code is None
    assert result.outcome.attempts_made == 1
    assert result.outcome.first_refusal is None
    assert len(load_director_calls(tmp_path / "run")) == 1

    outcome, _plan_dir = _commit(
        source, tmp_path, result.selection.model_dump(mode="json")
    )
    assert isinstance(outcome, SelectionCommitOutcome)


def test_non_semantic_refusal_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {RUNTIME_ENV: str(_runtime(tmp_path))}
    source = _input_for(tmp_path, env, facts_sha="ef" * 32)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(
            replies=[
                canonical_model_bytes(_proposal(CONTRADICTING)).decode("utf-8"),
                canonical_model_bytes(_proposal(CLEAN)).decode("utf-8"),
            ]
        ),
    )

    result = select_proposal_with_contradiction_retry(source)

    assert len(fake.calls) == 1
    assert len(result.attempts) == 1
    assert result.attempts[0].refusal_code == "source_binding_mismatch"
    assert result.outcome.attempts_made == 1
    assert len(load_director_calls(tmp_path / "run")) == 1

    outcome, plan_dir = _commit(
        source, tmp_path, result.selection.model_dump(mode="json")
    )
    assert isinstance(outcome, SelectionCommitRefusal)
    assert outcome.code == "source_binding_mismatch"
    assert load_index(plan_dir).versions == {}


def test_only_the_semantic_contradiction_is_retryable() -> None:
    assert is_contradiction_refusal(
        ValidationRefusal(
            validator="semantic",
            code="keep_remove_contradiction",
            detail="span carries both intents",
        )
    ) is True
    assert is_contradiction_refusal(
        ValidationRefusal(
            validator="semantic", code="identity_mismatch", detail="recomputed"
        )
    ) is False
    assert is_contradiction_refusal(
        ValidationRefusal(
            validator="schema", code="keep_remove_contradiction", detail="parse"
        )
    ) is False
    assert is_contradiction_refusal(
        ValidationRefusal(validator=None, code="stale_base", detail="old base")
    ) is False
