"""Todo 39 acceptance: the Editorial Director model/tool boundary.

Every model response is a RECORDED replay fixture keyed by request hash; no
network is ever attempted. The replay goldens are the FROZEN golden selection
tables (manifest-derived, pre-registered) — never authored from model output.
Each case proves one boundary: evidence-backed proposals pass, and every
forbidden responsibility/tool/data class is rejected with an explicit record.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.contracts.editorial_model import EditorialSelectionProposal, SelectionEntry
from services.editorial.director import EditorialDirector
from services.editorial.evidence import assemble_evidence
from services.editorial.pin import PinError, load_pin, load_replay_set
from services.editorial.prompt import (
    PROMPT_CONTRACT_VERSION,
    build_prompt,
    prompt_bundle_hash,
    request_hash,
)
from services.editorial.transport import (
    EditorialModelRefusal,
    EditorialStrictResponse,
    EditorialTransportFailure,
    LiveTransport,
    ReplayTransport,
    replay_response,
)
from services.foundation_io import canonical_model_bytes
from services.media_query.api import MediaQueryApi
from tests.editorial.support import (
    INJECTION_SUFFIX,
    REPLAY_SET_PATH,
    build_index,
    derive_replay_set,
    golden_proposal,
    load_manifest,
    manifest_request,
)

if TYPE_CHECKING:
    from services.editorial.models import DirectorRunResult

ALL_FIXTURE_IDS = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)


def recorded_transport(
    result_key: str, payload: bytes, served_by: str
) -> ReplayTransport:
    return ReplayTransport(
        {result_key: EditorialStrictResponse(payload=payload, served_by=served_by)}
    )


def run_replay(tmp_path: Path, fixture_id: str) -> tuple[DirectorRunResult, ReplayTransport, str]:
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    replay_set = load_replay_set(pin)
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        outcome = replay_response(replay_set, fixture_id)
        transport = ReplayTransport({key: outcome})
        result = EditorialDirector(transport=transport).run(request, api=api)
    return result, transport, key


# ------------------------------------------------------------------ happy


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_replay_proposals_match_frozen_golden_tables_exactly(
    tmp_path: Path, fixture_id: str
) -> None:
    result, transport, _key = run_replay(tmp_path, fixture_id)

    assert transport.calls == 1
    assert result.envelope.status == "proposal"
    assert result.proposal is not None
    golden = golden_proposal(fixture_id)
    assert result.proposal.selection == golden.selection
    assert result.proposal.confidence == golden.confidence
    assert result.proposal.proposal_id == golden.proposal_id
    assert result.proposal.actor_intent == "model"
    # every golden decision is reproduced, in the frozen table order
    assert [(e.segment_id, e.action, e.reason_code) for e in result.proposal.selection] == [
        (e.segment_id, e.action, e.reason_code) for e in golden.selection
    ]


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_metadata_is_recorded_exactly(tmp_path: Path, fixture_id: str) -> None:
    result, _transport, key = run_replay(tmp_path, fixture_id)
    pin = load_pin()
    replay_set = load_replay_set(pin)
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    payload = canonical_model_bytes(replay_set.proposal_for(fixture_id))
    assert result.envelope.request_hash == key
    assert result.envelope.prompt_contract_version == PROMPT_CONTRACT_VERSION
    assert result.envelope.episode_id == fixture_id
    assert result.envelope.model_role_id == pin.model_role_id
    assert result.envelope.response_hash == hashlib.sha256(payload).hexdigest()
    assert result.envelope.external_credentials == "none"
    assert result.metadata.pin_version == pin.pin_version
    assert result.metadata.requested_model == pin.model_id
    assert result.metadata.observed_model == f"replay:{replay_set.replay_set_id}"
    assert result.metadata.transport_kind == "replay"
    assert result.metadata.prompt_bundle_hash == prompt_bundle_hash(build_prompt(request))
    assert len(result.metadata.evidence_lineage) == 2  # transcript + dialogue artifacts
    assert result.policy.fixture_binding == "granted"
    assert result.policy.control_plane_decision == "allow"


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_same_request_is_byte_deterministic(tmp_path: Path, fixture_id: str) -> None:
    first, _t1, key1 = run_replay(tmp_path / "a", fixture_id)
    second, _t2, key2 = run_replay(tmp_path / "b", fixture_id)
    assert key1 == key2
    assert canonical_model_bytes(first) == canonical_model_bytes(second)


def test_evidence_bundle_corroborates_each_candidate_kind(tmp_path: Path) -> None:
    manifest = load_manifest("p1-ref-02-pauses-fillers")
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    with MediaQueryApi.open(episode.db_path) as api:
        bundle = assemble_evidence(api, request)
    by_id = {c.segment_id: c.method for c in bundle.corroborations}
    assert by_id["s1"] == "transcript_search"
    assert by_id["f1"] == "transcript_search"
    assert by_id["p1"] == "silence_overlap"
    assert by_id["p2"] == "silence_overlap"
    assert set(bundle.admissible_segment_ids) == {
        segment.segment_id for segment in manifest.transcript.segments
    }
    assert bundle.lineage == episode.lineage()


def test_replay_fixture_file_is_manifest_derived_and_pin_bound() -> None:
    derived = derive_replay_set()
    raw = REPLAY_SET_PATH.read_bytes()
    assert raw == canonical_model_bytes(derived), "replay set must re-derive from goldens"
    pin = load_pin()
    assert pin.replay_set_sha256 == hashlib.sha256(raw).hexdigest()
    assert derived.derived_from.source_sha256 == hashlib.sha256(
        Path(derived.derived_from.source_path).read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------- failures


def test_hallucinated_segment_reference_is_rejected(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        poisoned = EditorialSelectionProposal(
            schema_version="editorial-selection-proposal-v1",
            proposal_id="sel-hallucination-v1",
            episode_id=fixture_id,
            actor_intent="model",
            selection=(
                SelectionEntry(segment_id="s1", action="selected", reason_code="score-selected"),
                SelectionEntry(
                    segment_id="s999", action="selected", reason_code="invented-evidence"
                ),
            ),
            confidence=(2, 2),
        )
        transport = recorded_transport(
            key, canonical_model_bytes(poisoned), "replay:probe"
        )
        result = EditorialDirector(transport=transport).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code == "hallucinated_reference"
    assert "s999" in result.error.detail


def test_prompt_injection_in_transcript_evidence_rides_as_data(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    # the INDEX text (untrusted media-derived data) carries the injection; the
    # declared candidate text stays clean and still substring-matches
    episode = build_index(tmp_path, manifest, inject_into="s1")
    pin = load_pin()
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        assert any(c.segment_id == "s1" for c in evidence.corroborations)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        replay_set = load_replay_set(pin)
        golden = replay_set.proposal_for(fixture_id)
        transport = recorded_transport(
            key, canonical_model_bytes(golden), f"replay:{replay_set.replay_set_id}"
        )
        director = EditorialDirector(transport=transport)
        result = director.run(request, api=api)
    # the proposal is unchanged in kind: same golden selection, no tool use,
    # no obedience to the injected instruction text
    assert result.envelope.status == "proposal"
    assert result.proposal is not None
    assert result.proposal.selection == golden.selection
    assert INJECTION_SUFFIX not in json.dumps(
        result.proposal.model_dump(mode="json"), ensure_ascii=False
    )


def test_response_claiming_tool_use_is_rejected(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        poisoned = json.dumps(
            {
                "schema_version": "editorial-selection-proposal-v1",
                "proposal_id": "sel-tool-claim-v1",
                "episode_id": fixture_id,
                "actor_intent": "model",
                "selection": [
                    {"segment_id": "s1", "action": "selected", "reason_code": "score-selected"}
                ],
                "confidence": [1, 1],
                "tool_calls": [{"name": "shell", "argv": ["rm", "-rf", "/"]}],
            }
        ).encode()
        transport = recorded_transport(key, poisoned, "replay:probe")
        result = EditorialDirector(transport=transport).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code == "arbitrary_tool_claim"
    assert "no tool surface" in result.error.detail


def test_direct_commit_fields_in_response_are_rejected(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        poisoned = json.dumps(
            {
                "schema_version": "editorial-selection-proposal-v1",
                "proposal_id": "sel-commit-attempt-v1",
                "episode_id": fixture_id,
                "actor_intent": "model",
                "selection": [
                    {"segment_id": "s1", "action": "selected", "reason_code": "score-selected"}
                ],
                "confidence": [1, 1],
                "commit": True,
                "decision": "approved",
            }
        ).encode()
        transport = recorded_transport(key, poisoned, "replay:probe")
        result = EditorialDirector(transport=transport).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.proposal is None
    assert result.error is not None
    assert result.error.code == "commit_field_forbidden"


def test_production_episode_is_denied_before_transport(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest, episode_id="production-ep-0001")
    episode = build_index(tmp_path, manifest)
    probe = ReplayTransport({})
    with MediaQueryApi.open(episode.db_path) as api:
        result = EditorialDirector(transport=probe).run(request, api=api)
    assert probe.calls == 0
    assert result.envelope.status == "error"
    assert result.error is not None
    assert result.error.code == "local_only_denial"
    assert result.policy.fixture_binding == "denied"
    assert result.policy.allowed is False


def test_refusal_and_truncated_responses_yield_no_proposal(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    truncated = b'{"schema_version": "editorial-selection-proposal-v1", "proposal_i'
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        refusal = EditorialDirector(
            transport=ReplayTransport({key: EditorialModelRefusal(reason="insufficient evidence")})
        ).run(request, api=api)
        cut = EditorialDirector(
            transport=recorded_transport(key, truncated, "replay:probe")
        ).run(request, api=api)
    assert refusal.envelope.status == "refusal"
    assert refusal.proposal is None
    assert refusal.refusal_reason == "insufficient evidence"
    assert refusal.metadata.observed_model is None
    assert cut.envelope.status == "error"
    assert cut.error is not None
    assert cut.error.code == "truncated_response"


def test_malformed_json_response_is_error(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    pin = load_pin()
    with MediaQueryApi.open(episode.db_path) as api:
        evidence = assemble_evidence(api, request)
        key = request_hash(PROMPT_CONTRACT_VERSION, evidence.lineage, pin.pin_version)
        result = EditorialDirector(
            transport=recorded_transport(key, b"not json at all", "replay:probe")
        ).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.error is not None
    assert result.error.code == "malformed_json"


def test_replay_hash_drift_is_explicit_mismatch(tmp_path: Path) -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    request = manifest_request(manifest)
    episode = build_index(tmp_path, manifest)
    stale = ReplayTransport({"0" * 64: EditorialModelRefusal(reason="stale")})
    with MediaQueryApi.open(episode.db_path) as api:
        result = EditorialDirector(transport=stale).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.error is not None
    assert result.error.code == "transport_failure"
    assert result.error.transport_code == "replay_mismatch"


def test_stale_replay_set_hash_refused(tmp_path: Path) -> None:
    pin = load_pin()
    tampered = tmp_path / "replay.json"
    tampered.write_bytes(REPLAY_SET_PATH.read_bytes() + b" ")
    with pytest.raises(PinError, match="replay-set-hash-drift"):
        load_replay_set(pin, tampered)


def test_uncorroborated_declaration_is_evidence_incomplete(tmp_path: Path) -> None:
    manifest = load_manifest("p1-ref-01-clean-ja")
    request = manifest_request(manifest)
    # drop the transcript artifact from the index: declared texts uncorroborated
    from services.media_query.index import MEDIA_DB_NAME, MediaQueryIndex  # noqa: PLC0415
    from tests.editorial.support import _analysis_artifact  # noqa: PLC0415

    root = tmp_path / "bare"
    root.mkdir(parents=True)
    with MediaQueryIndex.open(root / MEDIA_DB_NAME) as index:
        index.upsert_analyzer_artifact(_analysis_artifact(manifest))
    with MediaQueryApi.open(root / MEDIA_DB_NAME) as api:
        result = EditorialDirector(transport=ReplayTransport({})).run(request, api=api)
    assert result.envelope.status == "error"
    assert result.error is not None
    assert result.error.code == "evidence_incomplete"


def test_live_transport_without_credentials_refuses_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EDITORIAL_DIRECTOR_CLOUD_FIXTURE", raising=False)
    monkeypatch.delenv("EDITORIAL_DIRECTOR_API_KEY", raising=False)
    live = LiveTransport()
    disabled = live.send("a" * 64)
    assert isinstance(disabled, EditorialTransportFailure)
    assert disabled.code == "live_disabled"
    monkeypatch.setenv("EDITORIAL_DIRECTOR_CLOUD_FIXTURE", "1")
    no_credentials = live.send("a" * 64)
    assert isinstance(no_credentials, EditorialTransportFailure)
    assert no_credentials.code == "no_credentials"
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "value-never-inspected")
    not_executable = live.send("a" * 64)
    assert isinstance(not_executable, EditorialTransportFailure)
    assert not_executable.code == "model_not_executable"
    assert "refusing to fabricate" in not_executable.detail


def test_record_carries_no_credential_values(tmp_path: Path) -> None:
    result, _transport, _key = run_replay(tmp_path, "p1-ref-01-clean-ja")
    dumped = canonical_model_bytes(result).decode()
    assert "API_KEY" not in dumped
    assert "api_key" not in dumped
    assert result.envelope.external_credentials == "none"
