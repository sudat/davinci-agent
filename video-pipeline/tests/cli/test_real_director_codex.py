"""Flat-rate codex-exec transport for the real-episode director (hermetic).

Covers the measured real-footage blocker (adopted-policy selection rebuild
typed-stops with policy-not-interpretable because the director only runs the
deterministic baseline): the director's live seam now also rides the pinned
``codex exec`` transport when the resolved editorial runtime says
``production_model`` + ``codex-exec``.

Hermetic by construction: a REAL ``MediaQueryApi`` over a manifest-derived
synthetic index (``tests/editorial/support`` — no network, no media), an
allowing production policy snapshot (operator-supplied equivalent), and a
fake codex runner — the real ``codex`` binary is never touched
(``make_codex_runner`` is stubbed at the module seam).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.cli import live_editorial_codex, real_director
from services.cli.episode_runner_rebuild import _director_interpretable
from services.cli.live_editorial_codex import CodexTransportGatedError
from services.cli.real_director import (
    RealDirectorError,
    resolve_director_route,
    select,
)
from services.cli.real_director_runtime import DirectorRoute
from services.cli.real_policy import (
    load_policy,
    local_only_policy,
    policy_for_grant,
    write_policy_snapshot,
)
from services.cli.real_report import RunFacts, build_report
from services.contracts.editorial_model import EditorialSelectionProposal, SelectionEntry
from services.contracts.primitives import ArtifactRef
from services.editorial.candidate_models import (
    AnalyzerSegmentRecord,
    CandidatePool,
    CandidateProvenance,
    CandidateSpan,
)
from services.editorial.models import AdoptedPolicySummaryV1
from services.editorial.pin import load_pin
from services.editorial.prompt import UNTRUSTED_DATA_NOTICE
from services.editorial.transport import CREDENTIALS_ENV, EditorialStrictResponse
from services.episode_cockpit.models import EpisodeEditorialGrantV1
from services.foundation_io import canonical_model_bytes
from services.ingest.eligibility import EligibilityResult
from services.policy.data_policy import authorize_cloud_transport
from tests.editorial.support import build_index, load_manifest

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig

FIXTURE_ID = "p1-ref-01-clean-ja"
RUNTIME_ENV = "EDITORIAL_RUNTIME_CONFIG"
NETWORK_ENV = "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
POLICY_MARKER = "SHIBUYA-FRAME-MARKER-42"


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


def _write_runtime(tmp_path: Path, name: str, mode: str, transport: str | None) -> Path:
    payload: dict[str, str] = {"mode": mode}
    if transport is not None:
        payload["transport"] = transport
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _operator_grant(episode_id: str = FIXTURE_ID) -> EpisodeEditorialGrantV1:
    """The operator's PRD §8 declaration the product path persists per episode."""

    return EpisodeEditorialGrantV1(
        episode_id=episode_id,
        granted=True,
        data_class="transcript",
        stage="editorial_direct",
        granted_at="2026-09-09T00:00:00+00:00",
        note="r3 verification lane",
    )


def _allowing_policy() -> ResolvedConfig:
    """Allowing snapshot via the REAL grant path (never a hand-built allowlist)."""

    return policy_for_grant(FIXTURE_ID, _operator_grant())


@dataclass(frozen=True, slots=True)
class DirectorSeam:
    pool: CandidatePool
    speech_ids: tuple[str, ...]
    speech_text: dict[str, str]
    index_path: str
    total_frames: int


def _seam(tmp_path: Path) -> DirectorSeam:
    manifest = load_manifest(FIXTURE_ID)
    episode = build_index(tmp_path, manifest)
    voiced = [s for s in manifest.transcript.segments if s.text.strip()]
    assert len(voiced) >= 1
    records = tuple(
        AnalyzerSegmentRecord(
            segment_id=s.segment_id,
            kind=s.kind,
            span=CandidateSpan(
                start_frame=s.span.start_frame,
                end_frame=s.span.end_frame,
                rate_num=30,
                rate_den=1,
                start_ms=s.span.start_frame * 1000 // 30,
                end_ms=s.span.end_frame * 1000 // 30,
            ),
            evidence=(ArtifactRef(artifact_id="art-seam-1", sha256="ab" * 32),),
            provenance=CandidateProvenance(
                analyzer_version="todo34-v1", rule_ids=("p1-pauses-v1",)
            ),
        )
        for s in voiced
    )
    pool = CandidatePool(
        source_id="src-test",
        edit_source_sha="cd" * 32,
        total_frames=manifest.edit_source.total_frames,
        segments=records,
    )
    return DirectorSeam(
        pool=pool,
        speech_ids=tuple(s.segment_id for s in voiced),
        speech_text={s.segment_id: s.text for s in voiced},
        index_path=str(episode.db_path),
        total_frames=manifest.edit_source.total_frames,
    )


def _canned_proposal(seam: DirectorSeam) -> EditorialSelectionProposal:
    return EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id=f"sel-{FIXTURE_ID}-codex-t1",
        episode_id=FIXTURE_ID,
        actor_intent="model",
        selection=tuple(
            SelectionEntry(
                segment_id=sid, action="selected", reason_code="codex-test-keep"
            )
            for sid in seam.speech_ids
        ),
        confidence=(len(seam.speech_ids), len(seam.speech_ids)),
    )


def _stub_codex(
    monkeypatch: pytest.MonkeyPatch, fake: FakeCodexRunner
) -> FakeCodexRunner:
    monkeypatch.setattr(
        live_editorial_codex, "make_codex_runner", lambda *a: fake
    )
    return fake


def _select_kwargs(seam: DirectorSeam, env: dict[str, str]) -> dict[str, object]:
    manifest = load_manifest(FIXTURE_ID)
    return {
        "episode_id": FIXTURE_ID,
        "source_id": "src-test",
        "total_frames": seam.total_frames,
        "rules": manifest.editorial_rules,
        "pool": seam.pool,
        "speech_ids": seam.speech_ids,
        "speech_text": seam.speech_text,
        "index_path": seam.index_path,
        "policy": _allowing_policy(),
        "env": env,
    }


def test_a_heuristic_runtime_stays_deterministic_baseline_with_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "heuristic.json", "heuristic_diagnostic", None)
    fake = _stub_codex(monkeypatch, FakeCodexRunner())
    env = {RUNTIME_ENV: str(runtime), CREDENTIALS_ENV: "fake-key-never-consulted"}

    outcome = select(**_select_kwargs(seam, env))  # type: ignore[arg-type]

    assert outcome.mode == "deterministic-baseline"
    assert outcome.transport == "deterministic-baseline"
    assert outcome.served_by == "deterministic-baseline-v1:no-model-involved"
    assert [e.segment_id for e in outcome.proposal.selection] == list(seam.speech_ids)
    assert all(e.action == "selected" for e in outcome.proposal.selection)
    assert fake.calls == []
    assert _director_interpretable(env, None) is False


def test_b_codex_runtime_serves_model_output_and_records_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    canned = _canned_proposal(seam)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(replies=[canonical_model_bytes(canned).decode("utf-8")]),
    )
    env = {RUNTIME_ENV: str(runtime)}

    assert resolve_director_route(env) == DirectorRoute(
        mode="live", transport="codex-exec"
    )
    outcome = select(**_select_kwargs(seam, env))  # type: ignore[arg-type]

    assert outcome.mode == "live"
    assert outcome.transport == "codex-exec"
    pin_model = load_pin().model_id
    assert outcome.served_by == f"codex-exec:{pin_model}"
    assert [(e.segment_id, e.action) for e in outcome.proposal.selection] == [
        (e.segment_id, e.action) for e in canned.selection
    ]
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == pin_model
    assert fake.calls[0]["images"] == ()


def test_c_openai_runtime_with_key_uses_existing_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "openai.json", "production_model", "openai-api")
    canned = _canned_proposal(seam)
    seen: list[dict[str, object]] = []

    class _FakeHttpTransport:
        def __init__(self, *, request: object, pin: object, env: object) -> None:
            seen.append({"request": request, "pin": pin, "env": env})

        def send(self, request_hash: str) -> EditorialStrictResponse:
            return EditorialStrictResponse(
                payload=canonical_model_bytes(canned),
                served_by="live:test-model",
            )

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("codex transport must not construct on the openai-api path")

    monkeypatch.setattr(real_director, "LiveHttpTransport", _FakeHttpTransport)
    monkeypatch.setattr(live_editorial_codex, "make_codex_runner", _boom)
    env = {
        RUNTIME_ENV: str(runtime),
        CREDENTIALS_ENV: "fake-key",
        NETWORK_ENV: "1",
    }

    assert resolve_director_route(env) == DirectorRoute(
        mode="live", transport="openai-api"
    )
    outcome = select(**_select_kwargs(seam, env))  # type: ignore[arg-type]

    assert outcome.mode == "live"
    assert outcome.transport == "openai-api"
    assert outcome.served_by == "live:test-model"
    assert [(e.segment_id, e.action) for e in outcome.proposal.selection] == [
        (e.segment_id, e.action) for e in canned.selection
    ]
    assert len(seen) == 1


def test_d_codex_gate_refusal_is_typed_with_zero_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    probes: list[str] = []

    def _refuse(*args: object, **kwargs: object) -> object:
        probes.append("gate")
        raise CodexTransportGatedError("codex-binary-missing", "no codex on PATH")

    monkeypatch.setattr(live_editorial_codex, "make_codex_runner", _refuse)
    env = {RUNTIME_ENV: str(runtime)}

    with pytest.raises(RealDirectorError) as exc_info:
        select(**_select_kwargs(seam, env))  # type: ignore[arg-type]

    assert exc_info.value.code == "codex-binary-missing"
    assert probes == ["gate"]


def test_e_adopted_policy_flows_through_codex_seam_without_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    canned = _canned_proposal(seam)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(replies=[canonical_model_bytes(canned).decode("utf-8")]),
    )
    env = {RUNTIME_ENV: str(runtime)}
    adopted = AdoptedPolicySummaryV1(
        decision="adopt", structure=f"導入は短く {POLICY_MARKER}"
    )

    assert _director_interpretable(env, None) is True
    kwargs = _select_kwargs(seam, env)
    kwargs["adopted_policy"] = adopted
    outcome = select(**kwargs)  # type: ignore[arg-type]

    assert outcome.mode == "live"
    assert outcome.transport == "codex-exec"
    assert len(fake.calls) == 1
    assert POLICY_MARKER in str(fake.calls[0]["prompt"])

    heuristic = _write_runtime(
        tmp_path, "heuristic.json", "heuristic_diagnostic", None
    )
    assert _director_interpretable({RUNTIME_ENV: str(heuristic)}, None) is False


def test_f_runtime_precedence_explicit_over_env_over_default(
    tmp_path: Path,
) -> None:
    heuristic = _write_runtime(
        tmp_path, "heuristic.json", "heuristic_diagnostic", None
    )
    production = _write_runtime(
        tmp_path, "production.json", "production_model", "codex-exec"
    )

    assert resolve_director_route({RUNTIME_ENV: str(heuristic)}) == DirectorRoute(
        mode="deterministic-baseline", transport="deterministic-baseline"
    )
    assert resolve_director_route(
        {RUNTIME_ENV: str(heuristic)}, production
    ) == DirectorRoute(mode="live", transport="codex-exec")
    with pytest.raises(RealDirectorError) as exc_info:
        resolve_director_route(
            {RUNTIME_ENV: str(tmp_path / "missing.json")}, None
        )
    assert exc_info.value.code == "editorial-runtime-unreadable"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mode": "quantum"}), encoding="utf-8")
    with pytest.raises(RealDirectorError) as bad_info:
        resolve_director_route({RUNTIME_ENV: str(bad)}, None)
    assert bad_info.value.code == "editorial-runtime-mode-invalid"


def test_g_grant_snapshot_carries_only_the_grantable_pair(tmp_path: Path) -> None:
    granted_file = write_policy_snapshot(FIXTURE_ID, tmp_path, _operator_grant())
    granted = load_policy(granted_file)
    assert [
        (entry.data_class, entry.stage, entry.fixture_only)
        for entry in granted.cloud_allowlist
    ] == [("transcript", "editorial_direct", False)]
    decision = authorize_cloud_transport(
        granted, data_class="transcript", stage="editorial_direct", episode_id=FIXTURE_ID
    )
    assert decision.allowed is True


def test_g2_bare_snapshot_stays_local_only(tmp_path: Path) -> None:
    out = tmp_path / "bare"
    out.mkdir()
    snapshot = load_policy(write_policy_snapshot(FIXTURE_ID, out, None))
    assert list(snapshot.cloud_allowlist) == []


def test_h_no_grant_denies_before_transport_with_grant_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    fake = _stub_codex(monkeypatch, FakeCodexRunner())
    env = {RUNTIME_ENV: str(runtime)}

    kwargs = _select_kwargs(seam, env)
    kwargs["policy"] = local_only_policy(FIXTURE_ID)
    with pytest.raises(RealDirectorError) as exc_info:
        select(**kwargs)  # type: ignore[arg-type]

    assert exc_info.value.code == "director_failed"
    assert "transport_code=None" in str(exc_info.value)
    assert "editorial-grant" in str(exc_info.value)
    assert fake.calls == []


def test_i_revoked_grant_denies_like_never_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    fake = _stub_codex(monkeypatch, FakeCodexRunner())
    env = {RUNTIME_ENV: str(runtime)}
    revoked = _operator_grant().model_copy(update={"granted": False})

    kwargs = _select_kwargs(seam, env)
    kwargs["policy"] = policy_for_grant(FIXTURE_ID, revoked)
    with pytest.raises(RealDirectorError) as exc_info:
        select(**kwargs)  # type: ignore[arg-type]

    assert exc_info.value.code == "director_failed"
    assert "transport_code=None" in str(exc_info.value)
    assert fake.calls == []


def test_j_granted_run_sends_transcript_with_untrusted_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seam = _seam(tmp_path)
    runtime = _write_runtime(tmp_path, "codex.json", "production_model", "codex-exec")
    canned = _canned_proposal(seam)
    fake = _stub_codex(
        monkeypatch,
        FakeCodexRunner(replies=[canonical_model_bytes(canned).decode("utf-8")]),
    )
    env = {RUNTIME_ENV: str(runtime)}

    outcome = select(**_select_kwargs(seam, env))  # type: ignore[arg-type]

    assert outcome.mode == "live"
    assert len(fake.calls) == 1
    prompt = str(fake.calls[0]["prompt"])
    assert UNTRUSTED_DATA_NOTICE in prompt
    assert any(text for text in seam.speech_text.values() if text in prompt)


def test_k_run_report_records_grant_provenance() -> None:
    eligibility = EligibilityResult(
        episode_id=FIXTURE_ID,
        status="supported",
        contract_id="talking-head-mvp-v1",
        reasons=(),
        human_gates=(),
    )
    granted_report = build_report(
        RunFacts(
            episode_id=FIXTURE_ID,
            eligibility=eligibility,
            edit_source_world_sha256="ab" * 32,
            editorial_grant=_operator_grant(),
        ),
        "ANALYZED",
    )
    assert granted_report.editorial_grant_granted is True
    assert granted_report.editorial_grant_data_class == "transcript"
    assert granted_report.editorial_grant_stage == "editorial_direct"
    assert granted_report.editorial_grant_granted_at == "2026-09-09T00:00:00+00:00"
    assert granted_report.editorial_grant_note == "r3 verification lane"

    bare_report = build_report(
        RunFacts(
            episode_id=FIXTURE_ID,
            eligibility=eligibility,
            edit_source_world_sha256="ab" * 32,
        ),
        "ANALYZED",
    )
    assert bare_report.editorial_grant_granted is None
    assert bare_report.editorial_grant_note is None
