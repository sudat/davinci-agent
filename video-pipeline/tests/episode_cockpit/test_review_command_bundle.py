"""Command-bundle proposal sets (工程2 rework): one plan fix, all drafts.

「一案の中の複数修正」 is ONE previewed set whose explicit fixes apply
TOGETHER — never 「3案」, never sequential single adoption (a single-draft
echo is a typed 422; the first adoption would consume the set and leave
the rest silently dropped). A mid-batch validation failure applies
NOTHING (brief §5.3: 失敗時に半分だけ採用済みにしない). Old saved sets
without the kind field ARE bundles. The alternatives counterpart lives in
test_review_alternatives.py.
"""

# allow: SIZE_OK — one command-bundle story per file (saved-set adoption
# authority, sequential atomicity, degrade-to-flagged, and the round-3
# all-or-nothing + journal boundary share the ONE TestClient fixture set;
# splitting scatters a single connection contract).

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit import review_apply as review_apply_module
from services.episode_cockpit import review_chat as review_chat_module
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_apply import apply_drafts
from services.episode_cockpit.review_chat import (
    AppliedCommand,
    ReviewChatContext,
    ReviewChatError,
    ReviewCommandKind,
    ReviewStoreLocation,
    _command_id,
    interpret_command,
)
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    ReviewLlmProposals,
)
from services.episode_cockpit.review_proposals import (
    ReviewProposalSet,
    effective_proposal_kind,
    resolve_authoritative_drafts,
)
from services.foundation_io import canonical_model_bytes
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    initialize_store,
    load_head,
)
from tests.review_command.support import manifest_plan, with_locked_field

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.review_command.commit import CommitOutcome
    from services.review_command.models import ReviewCommandProposal0C

UNCAPPED = "調整して"  # no command pattern, no reaction vocabulary → LLM bundle route
CHOICE_B = "Bが好き"
REMOVE = "この区間を削除して"


def _proposal(target: float) -> dict[str, object]:
    return {"command_kind": "remove_section", "target_seconds": target}


def _llm(*returns: list[dict]) -> object:
    state = {"n": 0}

    def call(text: str, context: ReviewChatContext, nearby: NearbyContext) -> dict:
        del text, nearby
        state["n"] += 1
        return {"proposals": list(returns[min(state["n"] - 1, len(returns) - 1)])}

    return call


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"], episodes_root=workspace["episodes_root"]
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed(workspace: dict[str, Path], episode_id: str) -> None:
    base = workspace["episodes_root"] / episode_id / "review"
    store = ReviewStoreLocation(log_path=base / "events.jsonl", plan_dir=base / "store")
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)


def _chat(client: TestClient, episode_id: str, text: str) -> dict[str, object]:
    response = client.post(f"/episodes/{episode_id}/review-chat", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, episode_id: str, payload: dict[str, object]):
    return client.post(f"/episodes/{episode_id}/review-chat/apply", json=payload)


def _jsonl(workspace: dict[str, Path], episode_id: str, name: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / name
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def _drafts_of(body: dict[str, object]) -> list[dict[str, object]]:
    """The (possibly absent) multi-draft list of a chat response, typed."""

    return cast("list[dict[str, object]]", body.get("drafts", []))


def test_bundle_three_fixes_all_served_and_applied_together(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm(
        [
            _proposal(12.0),
            {"command_kind": "keep_longer", "target_seconds": 6.0, "seconds_delta": 1.0},
            _proposal(3.0),
        ]
    )
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    assert len(_drafts_of(body)) == 3  # 破棄・間引き禁止: every named fix served
    assert body["proposal_kind"] == "command-bundle"
    sets = _jsonl(workspace, episode_id, "review-proposals.jsonl")
    assert sets[0]["proposal_kind"] == "command-bundle"
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 200, applied.text
    commands = applied.json()["applied_commands"]
    assert [command["target_seconds"] for command in commands] == [12.0, 6.0, 3.0]
    assert len(_jsonl(workspace, episode_id, "applied-commands.jsonl")) == 3
    consumed = _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl")
    assert consumed[0]["set_sequence"] == 1
    assert consumed[0]["outcome"] == "applied"


def test_llm_envelope_schema_is_uncapped_for_bundles() -> None:
    """The served schema no longer caps at maxItems 2: explicit fixes are
    never thinned (破棄・間引き禁止); the max-2 rule is an alternatives-route
    deterministic cap, not a schema bound."""

    bounds = ReviewLlmProposals.model_json_schema()["properties"]["proposals"]
    assert bounds["minItems"] == 1
    assert "maxItems" not in bounds


def test_single_draft_apply_on_bundle_is_typed_error(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm([_proposal(12.0), _proposal(6.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": [_drafts_of(body)[0]], "sequence": 1}
    )
    assert applied.status_code == 422
    assert applied.json()["error"]["code"] == "bundle-requires-full-apply"
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


def test_choice_on_bundle_is_typed_error(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm([_proposal(12.0), _proposal(6.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    _chat(client, episode_id, UNCAPPED)
    response = client.post(f"/episodes/{episode_id}/review-chat", json={"text": CHOICE_B})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "choice-requires-alternatives"


def test_mid_batch_validation_failure_applies_nothing(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm([_proposal(12.0), _proposal(999.0), _proposal(6.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "target-not-in-plan"
    assert "command 2 of 3" in error["detail"]  # the failing command is named
    assert not (workspace["episodes_root"] / episode_id / "applied-commands.jsonl").exists()
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []
    # the plan head never moved: a good apply afterwards lands on v2
    good = _chat_with_seconds(client, episode_id, REMOVE, 12.0)
    applied_good = _apply(
        client,
        episode_id,
        {"text": REMOVE, "at_seconds": 12.0, "drafts": [good["draft"]], "sequence": 2},
    )
    assert applied_good.status_code == 200, applied_good.text
    assert applied_good.json()["applied"]["result_plan_version"] == "v2"


def _chat_with_seconds(
    client: TestClient, episode_id: str, text: str, at_seconds: float
) -> dict[str, object]:
    response = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": text, "at_seconds": at_seconds}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _draft_json(kind: ReviewCommandKind, target: float) -> dict[str, object]:
    return {
        "schema_version": "cockpit-review-command-draft-v1",
        "command_id": _command_id(kind, target, None, "昔の入力"),
        "command_kind": kind,
        "text": "昔の入力",
        "target_seconds": target,
        "needs_confirmation": False,
    }


def test_old_saved_sets_without_kind_parse_as_command_bundle() -> None:
    line = json.dumps(
        {
            "schema_version": "cockpit-review-proposals-v1",
            "sequence": 1,
            "chat_sequence": 1,
            "base_plan_version": "v1",
            "drafts": [_draft_json("remove_section", 12.0), _draft_json("remove_section", 6.0)],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    saved = ReviewProposalSet.model_validate_json(line)
    assert effective_proposal_kind(saved) == "command-bundle"
    drafts = saved.drafts
    with pytest.raises(ReviewChatError) as exc:
        resolve_authoritative_drafts(
            text=drafts[0].text,
            at_seconds=None,
            drafts=(drafts[0],),
            sequence=None,
            sets=[saved],
            consumed=frozenset(),
            head_version=1,
        )
    assert exc.value.code == "bundle-requires-full-apply"  # old sets keep bundle rules


# ---------------------------------------------------------------------------
# P1-1 (rework round 2): sequential atomicity — a draft is validated against
# the plan state AFTER the earlier drafts applied, and any validation
# failure applies nothing at all (0 commits, 0 applied entries, unconsumed).
# ---------------------------------------------------------------------------


def _store(workspace: dict[str, Path], episode_id: str) -> ReviewStoreLocation:
    base = workspace["episodes_root"] / episode_id / "review"
    return ReviewStoreLocation(log_path=base / "events.jsonl", plan_dir=base / "store")


def test_bundle_command_failing_only_against_earlier_command_state_applies_nothing(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex repro: [remove@12.0, remove@13.0] — the first remove (item v3)
    makes frame 390 uncovered, so the SECOND command fails only against the
    FIRST command's resulting plan. The old pre-validation passed both
    against the original head and half-applied before erroring."""
    fake = _llm([_proposal(12.0), _proposal(13.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "target-not-in-plan"
    assert "command 2 of 2" in error["detail"]
    assert "nothing was applied" in error["detail"]
    # head UNCHANGED (still v1), no applied entries, set unconsumed
    store = _store(workspace, episode_id)
    assert load_head(store.log_path, store.plan_dir).version == 1
    assert not (workspace["episodes_root"] / episode_id / "applied-commands.jsonl").exists()
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


def test_three_draft_bundle_validates_each_against_previous_state(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3-draft bundle whose MIDDLE draft fails against draft-1's
    provisional state is all-or-nothing: the valid 3rd draft never commits."""
    fake = _llm([_proposal(13.0), _proposal(12.0), _proposal(3.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    assert len(_drafts_of(body)) == 3
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "target-not-in-plan"
    assert "command 2 of 3" in error["detail"]
    store = _store(workspace, episode_id)
    assert load_head(store.log_path, store.plan_dir).version == 1
    assert not (workspace["episodes_root"] / episode_id / "applied-commands.jsonl").exists()
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


def test_mid_commit_failure_restores_bundle_base_and_reports_honestly(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit that fails AFTER simulation passed (e.g. IO) rolls the plan
    back to the pre-bundle version via an honest restore commit, keeps the
    applied-commands trail, and names applied N / failed at N+1 / restored-to."""
    fake = _llm([_proposal(12.0), _proposal(6.0)])  # both valid sequentially
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)

    real_commit = review_chat_module.commit_command
    calls = {"n": 0}

    def flaky_commit(
        proposal: ReviewCommandProposal0C,
        decision: OperatorDecision0C,
        log_path: Path,
        plan_dir: Path,
    ) -> CommitOutcome:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ReviewCommitError("disk_full", "injected mid-commit failure")
        return real_commit(proposal, decision, log_path, plan_dir)

    monkeypatch.setattr(review_chat_module, "commit_command", flaky_commit)
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "bundle-commit-failed"
    assert "command 2 of 2" in error["detail"]
    assert "1 of 2" in error["detail"]  # applied 1 before the failure
    assert "restored to v3" in error["detail"]
    # honest trail: draft 1's applied entry stays, and the restore committed
    # a NEW version whose content equals the pre-bundle plan (v1)
    store = _store(workspace, episode_id)
    head = load_head(store.log_path, store.plan_dir)
    assert head.version == 3
    assert canonical_model_bytes(head.plan) == canonical_model_bytes(
        manifest_plan("p0c-remove-clear")
    )
    trail = _jsonl(workspace, episode_id, "applied-commands.jsonl")
    assert len(trail) == 1
    assert trail[0]["target_seconds"] == 12.0
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


# ---------------------------------------------------------------------------
# P1-3 (rework round 2): a bundle with an uninterpretable element NEVER
# serves the valid subset — the whole response degrades to ONE flagged
# draft with an honest reason (全件保持・半端防止).
# ---------------------------------------------------------------------------


def test_bundle_with_one_uninterpretable_element_serves_single_flagged_draft(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _llm(
        [
            _proposal(12.0),
            {"command_kind": "keep_longer", "target_seconds": 6.0, "seconds_delta": 1.0},
            {"command_kind": "frobnicate", "target_seconds": 3.0},  # outside the closed set
        ]
    )
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    # ONE flagged draft — never the 2 interpretable fixes served silently
    assert "drafts" not in body
    draft = cast("dict[str, object]", body["draft"])
    assert draft["command_kind"] is None
    assert draft["needs_confirmation"] is True
    assert "3件の修正のうち1件（frobnicate）" in str(draft["confirmation_reason"])  # noqa: RUF001 (JA notice)
    assert "全体を適用できません" in str(draft["confirmation_reason"])
    # apply is blocked on the flagged draft; the set stays unconsumed
    applied = _apply(client, episode_id, {"text": UNCAPPED, "drafts": [draft], "sequence": 1})
    assert applied.status_code == 422
    assert applied.json()["error"]["code"] == "draft-not-confirmed"
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


# ---------------------------------------------------------------------------
# P1-1 (rework round 3): a PREDICTED defer inside a BUNDLE — validation
# error or non-apply classification such as a lock conflict — used to pass
# THROUGH the simulation (recorded as "no plan change") and half-apply at
# commit. Now it is a whole-bundle validation failure. A single command
# keeps the honest defer instead (recorded intent, deferred flag).
# ---------------------------------------------------------------------------


def test_bundle_lock_conflicting_command_fails_whole_validation(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex repro (real HTTP): store seeded at v1 with item v2's ``order``
    field locked; the LLM saves a bundle [remove_section@12.0, remove_section
    @6.0]; apply with sequence. remove@12.0 clears (item v3); remove@6.0
    targets the ORDER-LOCKED v2 → conflict → predicted defer. The old
    behavior deferred THROUGH simulation and half-applied (422
    command-deferred BUT head=v2, v3/a3 deleted, 2 applied records, set
    unconsumed, no restore). Now: typed 422 naming the command + position
    + why, ZERO commits, version unchanged, applied-records EMPTY, set
    unconsumed."""
    fake = _llm([_proposal(12.0), _proposal(6.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    base = workspace["episodes_root"] / episode_id / "review"
    store = ReviewStoreLocation(log_path=base / "events.jsonl", plan_dir=base / "store")
    locked = with_locked_field(manifest_plan("p0c-remove-clear"), "v2", "order")
    initialize_store(locked, store.log_path, store.plan_dir)
    body = _chat(client, episode_id, UNCAPPED)
    drafts = _drafts_of(body)
    assert len(drafts) == 2
    applied = _apply(client, episode_id, {"text": UNCAPPED, "drafts": drafts, "sequence": 1})
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "bundle-command-deferred"
    assert "command 2 of 2" in error["detail"]
    assert str(drafts[1]["command_id"]) in error["detail"]
    assert "conflict" in error["detail"]  # the why: lock-conflict classification
    assert "v2" in error["detail"]
    assert "order" in error["detail"]
    # ZERO commits: head unchanged at v1, no second version file
    assert load_head(store.log_path, store.plan_dir).version == 1
    assert not (base / "store" / "plan-v2.json").exists()
    # applied-records EMPTY; the saved set UNCONSUMED
    assert not (workspace["episodes_root"] / episode_id / "applied-commands.jsonl").exists()
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


def test_single_command_defer_keeps_honest_recorded_intent(tmp_path: Path) -> None:
    """Scope guard for the P1-1 fix: the all-or-nothing rule is BUNDLE-only.
    A single lock-conflicting draft still defers honestly at commit — the
    intent is journaled with the deferred flag, no version commits, and the
    caller sees the typed command-deferred failure."""
    base = tmp_path / "review"
    store = ReviewStoreLocation(log_path=base / "events.jsonl", plan_dir=base / "store")
    locked = with_locked_field(manifest_plan("p0c-remove-clear"), "v2", "order")
    initialize_store(locked, store.log_path, store.plan_dir)
    draft = interpret_command("この区間を削除して", ReviewChatContext(at_seconds=6.0))
    with pytest.raises(ReviewChatError) as raised:
        apply_drafts([draft], episode_dir=tmp_path, store=store)
    assert raised.value.code == "command-deferred"
    assert load_head(store.log_path, store.plan_dir).version == 1
    journal = tmp_path / "applied-commands.jsonl"
    trail = [json.loads(line) for line in journal.read_bytes().splitlines()]
    assert len(trail) == 1
    assert trail[0]["deferred"] is True
    assert trail[0]["reason"] == "conflict"


# ---------------------------------------------------------------------------
# P1-2 (rework round 3): the applied-records journal write is INSIDE the
# mid-commit failure boundary. A journal OSError now restores the
# pre-bundle version and surfaces a typed 422; a failing RESTORE reports
# BOTH failures plus the loadable head version. Never a bare 500.
# ---------------------------------------------------------------------------


def _flaky_journal(fail_at_call: int) -> object:
    """Delegate to the REAL writer except at ``fail_at_call`` (1-based)."""

    real = review_apply_module.record_applied_command
    state = {"n": 0}

    def journal(episode_dir: Path, applied: AppliedCommand) -> None:
        state["n"] += 1
        if state["n"] == fail_at_call:
            raise OSError("injected journal failure")
        real(episode_dir, applied)

    return journal


def test_journal_write_failure_restores_and_reports_honestly(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex repro: the journal write sat OUTSIDE the failure boundary —
    an OSError on the second entry produced HTTP 500 with cmd1's commit
    left standing and no audit entry. Now: typed 422 (bundle-commit-failed)
    with applied 1 / journal-failed at command 2 / restored-to, head back
    at the pre-bundle content, cmd1's trail kept, set unconsumed."""
    fake = _llm([_proposal(12.0), _proposal(6.0)])  # both valid sequentially
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)
    monkeypatch.setattr(
        "services.episode_cockpit.review_apply.record_applied_command",
        _flaky_journal(fail_at_call=2),
    )
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "bundle-commit-failed"
    assert "command 2 of 2" in error["detail"]
    assert "journal" in error["detail"]
    assert "1 of 2" in error["detail"]
    assert "OSError: injected journal failure" in error["detail"]
    assert "restored to v4" in error["detail"]
    store = _store(workspace, episode_id)
    head = load_head(store.log_path, store.plan_dir)
    assert head.version == 4  # v1 → v2 (cmd1) → v3 (cmd2) → v4 (restore)
    assert canonical_model_bytes(head.plan) == canonical_model_bytes(
        manifest_plan("p0c-remove-clear")
    )
    trail = _jsonl(workspace, episode_id, "applied-commands.jsonl")
    assert len(trail) == 1
    assert trail[0]["target_seconds"] == 12.0
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []


def test_restore_failure_reports_both_failures_and_head(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore itself may raise: the typed error carries the ORIGINAL
    failure AND the rollback failure AND the loadable head version — and
    the route still answers 422, never 500."""
    fake = _llm([_proposal(12.0), _proposal(6.0)])
    monkeypatch.setattr("services.episode_cockpit.api.build_review_llm_call", lambda: fake)
    episode_id = _episode(client, source_folder)
    _seed(workspace, episode_id)
    body = _chat(client, episode_id, UNCAPPED)

    def failing_restore(
        restored_from_version: int,
        decision: OperatorDecision0C,
        log_path: Path,
        plan_dir: Path,
    ) -> object:
        del restored_from_version, decision, log_path, plan_dir
        raise OSError("injected restore failure")

    monkeypatch.setattr(
        "services.episode_cockpit.review_apply.record_applied_command",
        _flaky_journal(fail_at_call=2),
    )
    monkeypatch.setattr("services.episode_cockpit.review_apply.commit_restore", failing_restore)
    applied = _apply(
        client, episode_id, {"text": UNCAPPED, "drafts": _drafts_of(body), "sequence": 1}
    )
    assert applied.status_code == 422
    error = applied.json()["error"]
    assert error["code"] == "bundle-commit-failed"
    assert "OSError: injected journal failure" in error["detail"]  # original
    assert "rollback failed" in error["detail"]
    assert "OSError: injected restore failure" in error["detail"]  # the restore
    assert "current plan head is v3" in error["detail"]  # loadable, honest
    store = _store(workspace, episode_id)
    assert load_head(store.log_path, store.plan_dir).version == 3
    assert _jsonl(workspace, episode_id, "review-proposals-consumed.jsonl") == []
