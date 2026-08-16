from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import services.review_command.commit as commit_module
import services.review_command.reducer as reducer_module
from services.compile.phase0c import apply_command
from services.contracts.edit_plan_0c import EditPlan0C, ItemIdSelector0C, ReviewCommand0C
from services.contracts.primitives import SourceFrameSpan
from services.foundation_io import canonical_model_bytes
from services.review_command.commit import (
    CommitOutcome,
    commit_command,
    recover_orphan,
)
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    EventActor0C,
    EventStreamError,
    ReviewEvent0C,
    build_event,
    parse_event_stream,
)
from services.review_command.models import AdjustSourceSpanProposal0C
from services.review_command.reducer import (
    ReduceConflictError,
    ReduceResult,
    reduce,
    to_review_command,
)
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    initialize_store,
    load_head,
)
from services.review_command.validate import validate_proposal
from tests.review_command.support import (
    approve_editorial_plan_proposal,
    fixture_proposal,
    manifest_plan,
    with_locked_field,
)

if TYPE_CHECKING:
    from services.review_command.models import ReviewCommandProposal0C

GOLDEN = json.loads(Path("tests/goldens/reference/phase-0c/expected.json").read_bytes())
GOLDEN_FIXTURES = GOLDEN["fixtures"]


def operator_decision(decision_id: str = "dec-001") -> OperatorDecision0C:
    return OperatorDecision0C(decision_id=decision_id, actor_intent="operator")


def span_clear_proposal() -> AdjustSourceSpanProposal0C:
    proposal = fixture_proposal("p0c-span-clear")
    assert isinstance(proposal, AdjustSourceSpanProposal0C)
    return proposal


def approval_on_v2() -> ReviewCommandProposal0C:
    return approve_editorial_plan_proposal().model_copy(update={"base_plan_version": "v2"})


def recorded_then_applied(  # noqa: PLR0913 (explicit event-chain fields mirror the log contract)
    proposal: ReviewCommandProposal0C,
    *,
    sequence: int,
    previous_event_hash: str,
    base: str,
    result: str,
    actor: EventActor0C = "operator",
    decision_id: str = "dec-001",
) -> tuple[ReviewEvent0C, ReviewEvent0C]:
    recorded = build_event(
        sequence=sequence,
        kind="proposal_recorded",
        proposal=proposal,
        base_plan_version=base,
        applied=False,
        actor_intent=proposal.actor_intent,
        previous_event_hash=previous_event_hash,
    )
    applied = build_event(
        sequence=sequence + 1,
        kind="decision_applied",
        proposal=proposal,
        base_plan_version=base,
        result_plan_version=result,
        applied=True,
        actor_intent=actor,
        decision_id=decision_id,
        previous_event_hash=recorded.event_id,
    )
    return recorded, applied


def span_clear_stream(base: EditPlan0C) -> tuple[EditPlan0C, tuple[ReviewEvent0C, ...]]:
    proposal = span_clear_proposal()
    recorded, applied = recorded_then_applied(
        proposal,
        sequence=1,
        previous_event_hash=GENESIS_EVENT_HASH,
        base="v1",
        result="v2",
    )
    expected = apply_command(
        base,
        to_review_command(
            base,
            proposal,
            validate_proposal(base, proposal).candidate_item_ids,
        ),
    )
    return expected, (recorded, applied)


def init_store(tmp_path: Path) -> tuple[Path, Path, EditPlan0C]:
    store = tmp_path / "store"
    store.mkdir()
    log = store / "events.jsonl"
    base = manifest_plan("p0c-span-clear")
    initialize_store(base, log, store)
    return log, store, base


def golden_rows(result: ReduceResult) -> list[tuple[object, ...]]:
    return [
        (
            item.item_id,
            item.record_span.start_frame,
            item.record_span.end_frame,
            item.source.span.start_frame,
            item.source.span.end_frame,
            item.subtitle_text,
        )
        for track in result.ir.tracks
        for item in track.items
    ]


def test_ordered_commands_produce_expected_plan_and_ir() -> None:
    base = manifest_plan("p0c-span-clear")
    expected_plan, events = span_clear_stream(base)

    result = reduce(events, base)

    golden = GOLDEN_FIXTURES["p0c-span-clear"]
    assert result.plan.plan.plan_version == golden["resulting_plan_version"] == "v2"
    assert canonical_model_bytes(result.plan) == canonical_model_bytes(expected_plan)
    assert [item.item_id for item in result.plan.plan.items] == [
        entry["item_id"] for entry in golden["plan_items"]
    ]
    for item, expected in zip(result.plan.plan.items, golden["plan_items"], strict=True):
        assert item.span.start_frame == expected["span"]["start_frame"]
        assert item.span.end_frame == expected["span"]["end_frame"]
        assert item.subtitle_text == expected["subtitle_text"]
    expected_rows = [
        (
            entry["item_id"],
            entry["record_start"],
            entry["record_end"],
            entry["source_start"],
            entry["source_end"],
            entry["subtitle_text"],
        )
        for entry in golden["record_table"]
    ]
    assert golden_rows(result) == expected_rows
    assert result.versions_applied == ("v2",)


def test_deterministic_replay_twice_identical_hash() -> None:
    base = manifest_plan("p0c-span-clear")
    _, events = span_clear_stream(base)

    first = reduce(events, base)
    second = reduce(events, base)

    assert canonical_model_bytes(first.plan) == canonical_model_bytes(second.plan)
    assert canonical_model_bytes(first.ir) == canonical_model_bytes(second.ir)
    assert first.versions_applied == second.versions_applied
    assert first.deterministic_hash == second.deterministic_hash
    assert len(first.deterministic_hash) == 64


def test_atomic_local_versions_immutable_contiguous(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)

    first = commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    second = commit_command(approval_on_v2(), operator_decision("dec-002"), log, store)

    assert isinstance(first, CommitOutcome)
    assert isinstance(second, CommitOutcome)
    assert (first.version, first.deferred) == (2, False)
    assert (second.version, second.deferred) == (3, False)
    for version in (1, 2, 3):
        assert (store / f"plan-v{version}.json").is_file()
        assert (store / f"ir-v{version}.json").is_file()
    index = json.loads((store / "versions.json").read_bytes())
    assert sorted(index["versions"], key=int) == ["1", "2", "3"]
    assert index["versions"]["2"]["parent_version"] == 1
    assert index["versions"]["3"]["parent_version"] == 2
    plan_v1_before = (store / "plan-v1.json").read_bytes()
    plan_v2_before = (store / "plan-v2.json").read_bytes()

    replay_apply = commit_command(
        span_clear_proposal(), operator_decision("dec-009"), log, store
    )
    replay_approval = commit_command(approval_on_v2(), operator_decision("dec-009"), log, store)

    assert (replay_apply.version, replay_apply.idempotent) == (2, True)
    assert (replay_approval.version, replay_approval.idempotent) == (3, True)
    assert (store / "plan-v1.json").read_bytes() == plan_v1_before
    assert (store / "plan-v2.json").read_bytes() == plan_v2_before
    stream = parse_event_stream(log.read_bytes())
    assert len(stream) == 4


def test_existing_version_target_is_conflict_error(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)
    stray = store / "plan-v2.json"
    stray.write_bytes(b"{}")

    with pytest.raises(ReviewCommitError, match="version_exists"):
        commit_command(span_clear_proposal(), operator_decision(), log, store)

    assert stray.read_bytes() == b"{}"


def test_idempotent_duplicate_single_application() -> None:
    base = manifest_plan("p0c-span-clear")
    recorded, applied = recorded_then_applied(
        span_clear_proposal(),
        sequence=1,
        previous_event_hash=GENESIS_EVENT_HASH,
        base="v1",
        result="v2",
    )
    single = reduce((recorded, applied), base)
    doubled = reduce((recorded, applied, applied), base)

    assert doubled.versions_applied == single.versions_applied == ("v2",)
    assert doubled.deterministic_hash == single.deterministic_hash
    assert canonical_model_bytes(doubled.plan) == canonical_model_bytes(single.plan)


def test_duplicate_same_id_different_bytes_hard_conflict() -> None:
    base = manifest_plan("p0c-span-clear")
    _, (recorded, applied) = span_clear_stream(base)
    forged = applied.model_copy(update={"decision_id": "dec-evil"})

    with pytest.raises(ReduceConflictError) as excinfo:
        reduce((recorded, applied, forged), base)

    assert excinfo.value.code == "duplicate_event_conflict"


def test_reordered_noncommutative_stale_version() -> None:
    base = manifest_plan("p0c-span-clear")
    _, (recorded, applied) = span_clear_stream(base)
    remove_proposal = fixture_proposal("p0c-remove-clear")
    assert remove_proposal.base_plan_version == "v1"
    late_recorded, late_applied = recorded_then_applied(
        remove_proposal,
        sequence=3,
        previous_event_hash=applied.event_id,
        base="v1",
        result="v2",
    )

    with pytest.raises(ReduceConflictError) as excinfo:
        reduce((recorded, applied, late_recorded, late_applied), base)

    assert excinfo.value.code == "stale_version"


def test_stale_version_proposal_deferred_without_plan_file(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)
    applied = commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    assert applied.deferred is False
    stale = fixture_proposal("p0c-remove-clear")
    assert stale.base_plan_version == "v1"
    plan_v2_before = (store / "plan-v2.json").read_bytes()

    outcome = commit_command(stale, operator_decision("dec-002"), log, store)

    assert outcome.deferred is True
    assert outcome.reason == "stale_plan_version"
    assert outcome.version == 2
    assert not (store / "plan-v3.json").exists()
    assert (store / "plan-v2.json").read_bytes() == plan_v2_before
    head = load_head(log, store)
    assert head.version == 2
    stream = parse_event_stream(log.read_bytes())
    assert [event.kind for event in stream] == [
        "proposal_recorded",
        "decision_applied",
        "proposal_recorded",
        "command_deferred",
    ]


def test_lock_conflict_deferred_plan_unchanged(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    log = store / "events.jsonl"
    base = with_locked_field(manifest_plan("p0c-span-clear"), "v2", "span")
    initialize_store(base, log, store)
    plan_before = (store / "plan-v1.json").read_bytes()

    outcome = commit_command(span_clear_proposal(), operator_decision(), log, store)

    assert outcome.deferred is True
    assert outcome.reason == "conflict"
    assert (store / "plan-v1.json").read_bytes() == plan_before
    assert not (store / "plan-v2.json").exists()
    stream = parse_event_stream(log.read_bytes())
    assert [event.kind for event in stream] == ["proposal_recorded", "command_deferred"]


def test_direct_file_edit_foreign_plan(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)
    commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    tampered = json.loads((store / "plan-v2.json").read_bytes())
    tampered["plan"]["items"][0]["span"]["end_frame"] = 149
    (store / "plan-v2.json").write_text(json.dumps(tampered, sort_keys=True))

    with pytest.raises(ReviewCommitError, match="foreign_plan"):
        load_head(log, store)
    with pytest.raises(ReviewCommitError, match="foreign_plan"):
        commit_command(approval_on_v2(), operator_decision("dec-002"), log, store)


def test_defer_between_applies_leaves_plan_unchanged(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)
    first = commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    assert (first.version, first.deferred) == (2, False)
    plan_after_apply = (store / "plan-v2.json").read_bytes()

    stale = fixture_proposal("p0c-remove-clear")
    deferred = commit_command(stale, operator_decision("dec-002"), log, store)
    assert deferred.deferred is True
    assert (store / "plan-v2.json").read_bytes() == plan_after_apply

    approval = commit_command(approval_on_v2(), operator_decision("dec-003"), log, store)
    assert (approval.version, approval.deferred) == (3, False)
    assert (store / "plan-v3.json").read_bytes() == plan_after_apply


def test_model_authored_decision_refused(tmp_path: Path) -> None:
    base = manifest_plan("p0c-span-clear")
    recorded, applied = recorded_then_applied(
        span_clear_proposal(),
        sequence=1,
        previous_event_hash=GENESIS_EVENT_HASH,
        base="v1",
        result="v2",
        actor="model",
    )
    with pytest.raises(ReduceConflictError) as excinfo:
        reduce((recorded, applied), base)
    assert excinfo.value.code == "model_authored_decision"

    log, store, _base = init_store(tmp_path)
    model_decision = OperatorDecision0C(decision_id="dec-evil", actor_intent="model")
    with pytest.raises(ReviewCommitError, match="model_authored_decision"):
        commit_command(span_clear_proposal(), model_decision, log, store)
    assert parse_event_stream(log.read_bytes()) == ()
    assert not (store / "plan-v2.json").exists()
    with pytest.raises(ValidationError):
        OperatorDecision0C.model_validate(
            {"decision_id": "dec-x", "actor_intent": "root", "note": None}
        )


def test_malformed_event_stream_rejected() -> None:
    base = manifest_plan("p0c-span-clear")
    _, (recorded, applied) = span_clear_stream(base)
    honest = canonical_model_bytes(recorded) + b"\n" + canonical_model_bytes(applied) + b"\n"

    with pytest.raises(EventStreamError):
        parse_event_stream(honest + b'{"broken":\n')
    with pytest.raises(EventStreamError):
        parse_event_stream(
            canonical_model_bytes(applied) + b"\n" + canonical_model_bytes(recorded) + b"\n"
        )
    forged_line = json.loads(canonical_model_bytes(applied))
    forged_line["decision_id"] = "dec-tampered"
    with pytest.raises(EventStreamError):
        parse_event_stream(
            canonical_model_bytes(recorded)
            + b"\n"
            + json.dumps(forged_line, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )

    parsed = parse_event_stream(honest)
    assert [event.sequence for event in parsed] == [1, 2]
    assert reduce(parsed, base).versions_applied == ("v2",)


def test_crash_between_event_and_plan_write_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, store, _base = init_store(tmp_path)

    real_atomic_write = commit_module.atomic_write

    def crashing_atomic_write(path: Path, payload: bytes) -> None:
        if path.name == "plan-v2.json":
            raise OSError("simulated crash before plan write")
        real_atomic_write(path, payload)

    monkeypatch.setattr(commit_module, "atomic_write", crashing_atomic_write)
    with pytest.raises(OSError, match="simulated crash"):
        commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    monkeypatch.undo()

    assert not (store / "plan-v2.json").exists()
    with pytest.raises(ReviewCommitError, match="orphan_event"):
        load_head(log, store)

    recover_orphan(log, store)
    head = load_head(log, store)
    assert head.version == 2
    assert [item.item_id for item in head.plan.plan.items] == ["v1", "v2", "a1", "a2"]
    approval = commit_command(approval_on_v2(), operator_decision("dec-002"), log, store)
    assert (approval.version, approval.deferred) == (3, False)


def test_event_log_seal_detects_tampering(tmp_path: Path) -> None:
    log, store, _base = init_store(tmp_path)
    commit_command(span_clear_proposal(), operator_decision("dec-001"), log, store)
    seal = store / "events.jsonl.seal"
    sealed = json.loads(seal.read_bytes())
    sealed["sequence"] = 99
    seal.write_text(json.dumps(sealed, sort_keys=True))

    with pytest.raises(ReviewCommitError, match="broken_seal"):
        load_head(log, store)
    appended = log.read_bytes()
    log.write_bytes(appended + appended.splitlines()[0] + b"\n")
    with pytest.raises(ReviewCommitError, match="broken_stream"):
        load_head(log, store)


def test_purity_reducer_imports_and_no_io() -> None:
    source = Path(reducer_module.__file__).read_bytes()
    tree = ast.parse(source)
    forbidden = {
        "services.artifact_store",
        "services.job_runner",
        "services.execution",
        "services.evidence",
        "services.gates",
        "services.toolchain",
        "services.fixtures",
        "services.source_snapshot",
        "services.qa",
        "sqlite3",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported & forbidden == set()
    assert "sqlite" not in source.decode()

    base = manifest_plan("p0c-span-clear")
    _, events = span_clear_stream(base)

    def refuse_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reduce must not perform file IO")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("builtins.open", refuse_io)
    monkeypatch.setattr("os.open", refuse_io)
    try:
        result = reduce(events, base)
    finally:
        monkeypatch.undo()
    assert result.versions_applied == ("v2",)


def test_review_command_mapping_reuses_compiler_arithmetic() -> None:
    base = manifest_plan("p0c-span-clear")
    proposal = span_clear_proposal()
    outcome = validate_proposal(base, proposal)
    command = to_review_command(base, proposal, outcome.candidate_item_ids)

    assert isinstance(command, ReviewCommand0C)
    assert isinstance(command.target, ItemIdSelector0C)
    assert command.target.item_id == "v2"
    assert command.new_span == SourceFrameSpan(
        start_frame=150, end_frame=240, rate=base.frame_rate
    )
    folded = apply_command(base, command)
    assert [item.item_id for item in folded.plan.items] == ["v1", "v2", "a1", "a2"]
    assert folded.plan.plan_version == "v2"


def test_reduce_accepts_arbitrary_event_sequences() -> None:
    base = manifest_plan("p0c-span-clear")
    _, events = span_clear_stream(base)
    sequence: Sequence[ReviewEvent0C] = events
    assert reduce(sequence, base).versions_applied == ("v2",)
