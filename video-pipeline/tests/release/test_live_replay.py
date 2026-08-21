"""Offline fault-shape tests for the live fault-injecting replay (Todo 67).

Every fault route is exercised against fakes (scripted build attempts, a
controllable restart seam, a fake clock/policy measure); the ONE live test
lives in ``test_live_replay_live.py`` behind the ``resolve_live`` marker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.release.live_builds import (
    BuildInterrupted,
    ConnectionLike,
    InterruptiblePool,
    RetryLimiter,
    RetryLimitError,
    interruptible_connection,
)
from services.release.live_flow import (
    LiveSeams,
    parse_injections,
    parse_profiles,
    revalidate_live_replay,
    run_live_replay,
)
from services.release.live_guard import (
    LEASE_HOLDER,
    LEASE_RESOURCE,
    H1BindingError,
    LiveStateGuard,
    ResolveLease,
    StaleStateError,
    verify_h1_binding,
)
from services.release.live_injections import (
    false_success_route,
    partial_build_route,
    repeated_interruption_route,
    resolve_restart_route,
    stale_state_route,
)
from services.release.live_media import (
    anchor_indices,
    declared_render_policy,
    verify_render_claim,
)
from services.release.live_models import (
    AnchorFrame,
    FinalRenderObservation,
    H1Evidence,
    LeaseEvidence,
    LiveReplaySummary,
    ProfileBuildResult,
    ProfileSwapEvidence,
    RestartObservation,
)
from services.release.staging import sha256_bytes
from services.resolve_bridge.connection import ProjectApi, TimelineApi, VersionBinding

HEX64 = "1" * 64
OTHER_HEX64 = "2" * 64


def _write_binding_extract(tmp_path: Path, *, git_sha: str) -> Path:
    """A minimal valid candidate extract: identity + manifest + H1 binding."""

    from services.release.staging import build_h1_binding  # noqa: PLC0415

    tmp_path.mkdir(parents=True, exist_ok=True)
    extract = tmp_path / "extract"
    checkpoint = tmp_path / "checkpoint-result.json"
    checkpoint.write_bytes(
        json.dumps(
            {
                "schema_version": "operator-checkpoint-v1",
                "record_type": "operator_checkpoint",
                "purpose": "EDITORIAL_APPROVED",
                "fixture_only": False,
                "episode_id": "ep-live-replay",
                "edit_source_world_sha256": HEX64,
                "final_plan_sha256": HEX64,
                "final_ir_sha256": HEX64,
                "final_preview_sha256": HEX64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    (tmp_path / "h1").mkdir(exist_ok=True)
    (tmp_path / "h1" / "checkpoint-result.json").write_bytes(checkpoint.read_bytes())
    binding_bytes, _ = build_h1_binding(tmp_path, git_sha)
    inputs = extract / "inputs" / "h1"
    inputs.mkdir(parents=True)
    (inputs / "binding.json").write_bytes(binding_bytes)
    (inputs / "checkpoint-result.json").write_bytes(checkpoint.read_bytes())
    from services.release.manifest import build_manifest, manifest_bytes  # noqa: PLC0415
    from services.release.models import IDENTITY_NAME, ReleaseIdentity  # noqa: PLC0415

    identity = ReleaseIdentity(
        git_sha=git_sha,
        source_tree_sha256=HEX64,
        plan_sha256=HEX64,
        start_work_ledger_suffix_sha256=HEX64,
        execution_ledger_sha256=HEX64,
    )
    (extract / IDENTITY_NAME).write_bytes(canonical_model_bytes(identity))
    (extract / "manifest.json").write_bytes(manifest_bytes(build_manifest(extract)))
    return extract


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


@dataclass
class FakeConnection:
    binding_name: str = "21.0.4"
    manager: FakeManager | None = None

    @property
    def binding(self) -> VersionBinding:
        return VersionBinding(
            product_name="DaVinci Resolve Studio",
            version_core=self.binding_name,
            build_number=4,
            version_string=f"DaVinci Resolve Studio {self.binding_name}B0004",
        )

    def project_manager(self) -> FakeManager:
        assert self.manager is not None
        return self.manager


class FakePool:
    def __init__(self) -> None:
        self.placements = 0

    def CreateEmptyTimeline(self, name: str) -> TimelineApi | None:
        return None

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[object]:
        self.placements += len(clip_infos)
        return [object() for _ in clip_infos]


class FakeProject:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    def GetName(self) -> str:
        return "__fvp_test__fake"

    def GetMediaPool(self) -> FakePool:
        return self._pool

    def GetTimelineCount(self) -> int:
        return 0

    def GetTimelineByIndex(self, index: int) -> TimelineApi | None:
        return None

    def SetCurrentTimeline(self, timeline: object) -> bool:
        return True


class FakeManager:
    def __init__(self, project: FakeProject) -> None:
        self._project = project

    def CreateProject(self, project_name: str) -> FakeProject:
        return self._project

    def LoadProject(self, project_name: str) -> FakeProject:
        return self._project

    def GetCurrentProject(self) -> FakeProject:
        return self._project

    def CloseProject(self, project: ProjectApi) -> bool:
        return True

    def DeleteProject(self, project_name: str) -> bool:
        return True

    def SaveProject(self) -> bool:
        return True

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return []


def _fake_connection_with_pool() -> tuple[FakeConnection, FakePool]:
    pool = FakePool()
    connection = FakeConnection(manager=FakeManager(FakeProject(pool)))
    return connection, pool


def _scripted_build(script: list[str | BaseException]) -> tuple[Callable[[], str], list[object]]:
    """A BuildAttempt fake replaying ``script`` entries per call."""

    calls: list[object] = []

    def attempt() -> str:
        entry = script[len(calls)]
        calls.append(entry)
        if isinstance(entry, BaseException):
            raise entry
        return str(entry)

    return attempt, calls


# --------------------------------------------------------------------- CLI ---


def _stub_summary() -> LiveReplaySummary:
    policy = declared_render_policy()
    return LiveReplaySummary(
        extract_path="extract",
        extract_git_sha="a" * 40,
        candidate_id=HEX64,
        h1=H1Evidence(
            binding_path="binding.json",
            binding_sha256=HEX64,
            episode_id="e",
            git_sha="a" * 40,
        ),
        lease=LeaseEvidence(
            db_path="lease.sqlite3", resource="r", holder="h", ttl_seconds=1,
            acquired=True, released=True,
        ),
        injections=(),
        profile_swap=ProfileSwapEvidence(
            profiles=(), structural_equal=True, presentation_differences=(), passed=True
        ),
        final_render=FinalRenderObservation(
            path="render/final.mp4",
            sha256=HEX64,
            source_profile="a",
            anchors=(),
            anchor_extraction_argv=(),
            policy_declared=policy, policy_measured=policy, policy_verified=True,
        ),
        verdict="passed",
        failure_codes=(),
    )


def _patch_live_cli(monkeypatch: pytest.MonkeyPatch, fake_run: object) -> None:
    monkeypatch.setattr("services.release.live_flow.run_live_replay", fake_run)
    monkeypatch.setattr("services.release.live_flow.revalidate_live_replay", lambda out,
        seams: None)
    monkeypatch.setattr("services.release.live_wiring.resolve_host_report",
        lambda out: Path("r.json"))
    monkeypatch.setattr("services.release.live_wiring.pinned_media_bins", lambda: (Path("f"),
        Path("p")))
    monkeypatch.setattr("services.release.live_wiring.production_seams", lambda **kwargs: object())


def test_10_live_mode_parses_exact_f3_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.release import replay as replay_cli  # noqa: PLC0415

    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> LiveReplaySummary:
        captured.update(kwargs)
        return _stub_summary()

    _patch_live_cli(monkeypatch, fake_run)
    exit_code = replay_cli.main(
        [
            "--candidate-extract",
            str(tmp_path / "extract"),
            "--h1-binding",
            str(tmp_path / "extract/inputs/h1/binding.json"),
            "--inject",
            "partial-build,resolve-restart,stale-state,false-success,repeated-interruption",
            "--profile-swap",
            "a,b",
            "--out",
            str(tmp_path / "out"),
            "--uv-bin",
            str(tmp_path / "uv"),
        ]
    )
    assert exit_code == 0
    assert captured["injections"] == (
        "partial-build",
        "resolve-restart",
        "stale-state",
        "false-success",
        "repeated-interruption",
    )
    assert captured["profiles"] == ("a", "b")


def test_11_live_mode_requires_candidate_extract(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch) -> None:
    from services.release import replay as replay_cli  # noqa: PLC0415

    _patch_live_cli(monkeypatch, lambda **kwargs: None)
    assert (
        replay_cli.main(
            [
                "--staging",
                str(tmp_path / "staging"),
                "--h1-binding",
                str(tmp_path / "b.json"),
                "--inject",
                "stale-state",
                "--profile-swap",
                "a",
                "--out",
                str(tmp_path / "out"),
                "--uv-bin",
                str(tmp_path / "uv"),
            ]
        )
        == 2
    )


def test_12_live_mode_requires_all_live_flags(tmp_path: Path) -> None:
    from services.release import replay as replay_cli  # noqa: PLC0415

    assert (
        replay_cli.main(
            [
                "--candidate-extract",
                str(tmp_path / "extract"),
                "--inject",
                "stale-state",
                "--profile-swap",
                "a",
                "--out",
                str(tmp_path / "out"),
                "--uv-bin",
                str(tmp_path / "uv"),
            ]
        )
        == 2
    )


def test_13_unknown_injection_refused() -> None:
    with pytest.raises(ValueError, match="unknown injection"):
        parse_injections("partial-build,bogus")
    assert parse_injections("stale-state,false-success") == ("stale-state", "false-success")


def test_14_unknown_or_duplicate_profile_refused() -> None:
    with pytest.raises(ValueError, match="profile"):
        parse_profiles("a,c")
    with pytest.raises(ValueError, match="profile"):
        parse_profiles("a,a")
    with pytest.raises(ValueError, match="profile"):
        parse_profiles("")
    assert parse_profiles("b,a") == ("b", "a")


def test_15_offline_mode_unchanged_without_live_flags() -> None:
    from services.release import replay as replay_cli  # noqa: PLC0415

    with pytest.raises(SystemExit) as exited:
        replay_cli.main(["--staging", "staging-x", "--out", "out-y"])
    assert exited.value.code == 2


# ------------------------------------------------------------------- guard ---


def test_20_guard_commits_under_current_plan_sha(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    path = guard.commit(tmp_path, "record.json", b"payload", plan_sha256=HEX64)
    assert path.read_bytes() == b"payload"


def test_21_guard_rejects_superseded_and_unknown_sha(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    guard.rotate(OTHER_HEX64)
    with pytest.raises(StaleStateError) as superseded:
        guard.commit(tmp_path, "old.json", b"payload", plan_sha256=HEX64)
    assert superseded.value.code == "superseded_plan_sha"
    with pytest.raises(StaleStateError) as stale:
        guard.commit(tmp_path, "unknown.json", b"payload", plan_sha256="3" * 64)
    assert stale.value.code == "stale_plan_sha"
    assert not (tmp_path / "old.json").exists()
    assert not (tmp_path / "unknown.json").exists()


def test_22_lease_acquire_release_roundtrip(tmp_path: Path) -> None:
    clock = FakeClock()
    lease = ResolveLease(tmp_path / "lease.sqlite3", now=clock)
    lease.acquire()
    rows = lease.holders()
    assert rows == [(LEASE_RESOURCE, LEASE_HOLDER)]
    other = ResolveLease(
        tmp_path / "lease.sqlite3", now=clock, resource=LEASE_RESOURCE, holder="other-holder"
    )
    from services.job_runner.state_errors import StateStoreError  # noqa: PLC0415

    with pytest.raises(StateStoreError, match="lease-held"):
        other.acquire()
    lease.release()
    assert lease.holders() == []


def test_23_h1_binding_verification_rejects_git_drift(tmp_path: Path) -> None:
    extract = _write_binding_extract(tmp_path, git_sha="a" * 40)
    evidence = verify_h1_binding(extract, extract / "inputs/h1/binding.json")
    assert evidence.git_sha == "a" * 40
    drifted = extract / "identity.json"
    payload = json.loads(drifted.read_bytes())
    payload["git_sha"] = "b" * 40
    drifted.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with pytest.raises(H1BindingError, match="git_sha"):
        verify_h1_binding(extract, extract / "inputs/h1/binding.json")


# ------------------------------------------------------------------- media ---


def test_30_anchor_indices() -> None:
    assert anchor_indices(600) == {
        "frame-0": 0,
        "frame-25pct": 150,
        "frame-50pct": 300,
        "frame-75pct": 450,
        "frame-last": 599,
    }
    single = anchor_indices(1)
    assert len(set(single.values())) == 1


def test_31_claim_missing_output(tmp_path: Path) -> None:
    result = verify_render_claim(
        tmp_path / "missing.mp4",
        declared_sha256=HEX64,
        declared_policy=declared_render_policy(),
        measure=lambda media: declared_render_policy(),
    )
    assert result.outcome == "claim_missing_output"


def test_32_claim_hash_mismatch(tmp_path: Path) -> None:
    media = tmp_path / "render.mp4"
    media.write_bytes(b"real-render-bytes")
    tampered = tmp_path / "tampered.mp4"
    tampered.write_bytes(b"real-render-byteS")
    result = verify_render_claim(
        tampered,
        declared_sha256=sha256_bytes(media.read_bytes()),
        declared_policy=declared_render_policy(),
        measure=lambda source: declared_render_policy(),
    )
    assert result.outcome == "claim_hash_mismatch"


def test_33_claim_policy_mismatch(tmp_path: Path) -> None:
    media = tmp_path / "render.mp4"
    media.write_bytes(b"real-render-bytes")
    lying = declared_render_policy().model_copy(update={"frame_count": 999})
    result = verify_render_claim(
        media,
        declared_sha256=sha256_bytes(media.read_bytes()),
        declared_policy=lying,
        measure=lambda source: declared_render_policy(),
    )
    assert result.outcome == "claim_policy_mismatch"


def test_34_claim_verified_control(tmp_path: Path) -> None:
    media = tmp_path / "render.mp4"
    media.write_bytes(b"real-render-bytes")
    result = verify_render_claim(
        media,
        declared_sha256=sha256_bytes(media.read_bytes()),
        declared_policy=declared_render_policy(),
        measure=lambda source: declared_render_policy(),
    )
    assert result.outcome == "verified"
    assert result.measured_policy == declared_render_policy()


# ------------------------------------------------------------------ builds ---


def test_40_interruptible_pool_raises_at_kth_placement() -> None:
    connection, _pool = _fake_connection_with_pool()
    assert connection.manager is not None
    seam = interruptible_connection(connection, interrupt_at=2)
    seam_project = seam.project_manager().CreateProject("__fvp_test__x")
    assert seam_project is not None
    seam_pool = cast("InterruptiblePool", seam_project.GetMediaPool())
    seam_pool.AppendToTimeline([{"mediaType": 1}])
    seam_pool.AppendToTimeline([{"mediaType": 1}])
    with pytest.raises(BuildInterrupted):
        seam_pool.AppendToTimeline([{"mediaType": 1}])


def test_41_retry_limiter_bounds_and_refuses() -> None:
    limiter = RetryLimiter(max_attempts=3)
    for _ in range(3):
        limiter.require_can_attempt()
        limiter.record_attempt()
    with pytest.raises(RetryLimitError):
        limiter.require_can_attempt()


# ------------------------------------------------------------------ routes ---


def test_50_partial_build_route_recovers_clean(tmp_path: Path) -> None:
    attempt, calls = _scripted_build([BuildInterrupted("kill seam after 2 items"),
        "__fvp_test__ok"])
    guard = LiveStateGuard(HEX64)
    outcome = partial_build_route(
        interrupted=attempt,
        clean=lambda: "__fvp_test__rebuild",
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert outcome.passed
    assert outcome.outcome == "recovered_clean_rebuild"
    committed = sorted(path.name for path in (tmp_path / "committed").glob("*.json"))
    assert committed == ["build-__fvp_test__rebuild.json"]
    assert len(calls) == 1


def test_51_partial_build_route_fails_when_recovery_interrupts(tmp_path: Path) -> None:
    attempt, _calls = _scripted_build([BuildInterrupted("first")])
    guard = LiveStateGuard(HEX64)
    outcome = partial_build_route(
        interrupted=attempt,
        clean=lambda: (_ for _ in ()).throw(BuildInterrupted("again")),
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert not outcome.passed
    assert outcome.outcome == "recovery_failed"
    committed_dir = tmp_path / "committed"
    half = list(committed_dir.glob("*.json")) if committed_dir.is_dir() else []
    assert not half


def test_52_partial_build_route_fails_when_injection_did_not_interrupt(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    outcome = partial_build_route(
        interrupted=lambda: "__fvp_test__no-interrupt",
        clean=lambda: "__fvp_test__rebuild",
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert not outcome.passed
    assert outcome.outcome == "interrupt_not_observed"


def test_53_repeated_interruption_bounded_recovery(tmp_path: Path) -> None:
    attempt, _calls = _scripted_build(
        [BuildInterrupted("abort-1"), BuildInterrupted("abort-2"), "__fvp_test__third"]
    )
    guard = LiveStateGuard(HEX64)
    outcome = repeated_interruption_route(
        attempts=(attempt, attempt, attempt),
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert outcome.passed
    assert outcome.outcome == "bounded_recovery_within_retry_limit"
    record = json.loads((tmp_path / "repeated-interruption.json").read_bytes())
    assert record["aborts"] == 2
    assert record["recovered_on_attempt"] == 3
    assert record["retry_limit_enforced"] is True


def test_54_repeated_interruption_exhaustion_fails(tmp_path: Path) -> None:
    def always_abort() -> str:
        raise BuildInterrupted("abort")

    guard = LiveStateGuard(HEX64)
    outcome = repeated_interruption_route(
        attempts=(always_abort, always_abort, always_abort),
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert not outcome.passed
    assert outcome.outcome == "recovery_exhausted_retry_limit"


def test_55_stale_state_route(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    outcome = stale_state_route(
        guard=guard,
        evidence_dir=tmp_path,
        superseded_sha=HEX64,
        current_sha=OTHER_HEX64,
        unknown_sha="3" * 64,
    )
    assert outcome.passed
    assert outcome.outcome == "typed_rejection_no_silent_use"
    assert not (tmp_path / "stale-artifact.json").exists()
    assert (tmp_path / "current-artifact.json").exists()


def test_56_false_success_route(tmp_path: Path) -> None:
    render = tmp_path / "final.mp4"
    render.write_bytes(b"real-render-bytes" * 100)
    outcome = false_success_route(
        render_path=render,
        declared_policy=declared_render_policy(),
        measure=lambda source: declared_render_policy(),
        evidence_dir=tmp_path,
    )
    assert outcome.passed
    assert outcome.outcome == "tamper_detected_by_recompute"
    record = json.loads((tmp_path / "false-success.json").read_bytes())
    assert record["hash_tamper"] == "claim_hash_mismatch"
    assert record["policy_lie"] == "claim_policy_mismatch"
    assert record["honest_control"] == "verified"


def test_57_restart_route_bounded_reconnect(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    new_connection = FakeConnection()
    observation = RestartObservation(
        quit_method="apple-event-quit",
        down_probe_error="BridgeUnavailable: scriptapp('Resolve') returned None",
        relaunch_seconds=42,
        binding_same=True,
    )
    rebuilds: list[object] = []

    def restart() -> tuple[RestartObservation, ConnectionLike]:
        return observation, cast("ConnectionLike", new_connection)

    outcome = resolve_restart_route(
        restart=restart,
        rebuild=lambda connection: rebuilds.append(connection) or "__fvp_test__resumed",
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert outcome.passed
    assert outcome.outcome == "bounded_reconnect_resumed"
    assert rebuilds == [new_connection]


def test_58_restart_route_binding_drift_fails(tmp_path: Path) -> None:
    guard = LiveStateGuard(HEX64)
    drifted = FakeConnection()
    drifted.binding_name = "21.0.5"

    def restart() -> tuple[RestartObservation, ConnectionLike]:
        observation = RestartObservation(
            quit_method="apple-event-quit",
            down_probe_error="BridgeUnavailable: down",
            relaunch_seconds=10,
            binding_same=False,
        )
        return observation, cast("ConnectionLike", drifted)

    outcome = resolve_restart_route(
        restart=restart,
        rebuild=lambda connection: "__fvp_test__never",
        guard=guard,
        evidence_dir=tmp_path,
        plan_sha256=HEX64,
    )
    assert not outcome.passed
    assert outcome.outcome == "binding_drift_after_restart"


# -------------------------------------------------------------------- flow ---


@dataclass
class FlowHarness:
    seams: LiveSeams
    out: Path
    calls: dict[str, int]
    renders: dict[str, bytes]
    extract: Path = Path()


def _flow_harness(tmp_path: Path, *, profiles: tuple[str, ...] = ("a", "b")) -> FlowHarness:
    extract = _write_binding_extract(tmp_path / "extract", git_sha="c" * 40)
    out = tmp_path / "out"
    calls: dict[str, int] = {"connect": 0, "restart": 0, "build": 0, "profile": 0, "cleanup": 0}
    renders = {
        profile: f"render-bytes-{profile}".encode() for profile in profiles
    }
    declared = declared_render_policy()

    def connect() -> ConnectionLike:
        calls["connect"] += 1
        return cast("ConnectionLike", FakeConnection())

    def restart(_connection: ConnectionLike) -> tuple[RestartObservation, ConnectionLike]:
        calls["restart"] += 1
        observation = RestartObservation(
            quit_method="apple-event-quit",
            down_probe_error="BridgeUnavailable: down",
            relaunch_seconds=5,
            binding_same=True,
        )
        return observation, FakeConnection()

    def injection_build(_connection: ConnectionLike, interrupt_at: int | None) -> str:
        calls["build"] += 1
        if interrupt_at is not None:
            raise BuildInterrupted(f"kill seam after {interrupt_at} items")
        return f"__fvp_test__build-{calls['build']}"

    def profile_build(
        _connection: ConnectionLike, profile: str, work: Path
    ) -> ProfileBuildResult:
        calls["profile"] += 1
        render = work / "render-final.mp4"
        render.parent.mkdir(parents=True, exist_ok=True)
        render.write_bytes(renders[profile])
        return ProfileBuildResult(
            render_path=render,
            derivatives={"audio_mix": sha256_bytes(f"mix-{profile}".encode()),
                "overlay": OTHER_HEX64},
        )

    def cleanup(_connection: ConnectionLike) -> tuple[str, ...]:
        calls["cleanup"] += 1
        return ()

    seams = LiveSeams(
        connect=connect,
        restart=restart,
        injection_build=injection_build,
        profile_build=profile_build,
        measure=lambda media: declared,
        anchors=lambda media, out_dir, indices: tuple(
            AnchorFrame(position=position, frame_index=index,
                frame_sha256=sha256_bytes(f"{index}".encode()))
            for position, index in indices.items()
        ),
        anchor_argv=lambda media, out_dir, indices: ("ffmpeg", "select"),
        now=FakeClock(),
        cleanup=cleanup,
    )
    return FlowHarness(seams=seams, out=out, calls=calls, renders=renders, extract=extract)


def test_70_full_flow_with_fake_seams_passes(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=("partial-build", "resolve-restart", "stale-state", "false-success",
            "repeated-interruption"),
        profiles=("a", "b"),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.verdict == "passed"
    assert summary.failure_codes == ()
    assert (harness.out / "run-summary.json").is_file()
    assert (harness.out / "render/final.mp4").read_bytes() == harness.renders["b"]
    assert summary.final_render.policy_verified
    assert len(summary.final_render.anchors) == 5
    outcomes = {row.route: row.outcome for row in summary.injections}
    assert outcomes["partial-build"] == "recovered_clean_rebuild"
    assert outcomes["resolve-restart"] == "bounded_reconnect_resumed"
    assert outcomes["stale-state"] == "typed_rejection_no_silent_use"
    assert outcomes["false-success"] == "tamper_detected_by_recompute"
    assert outcomes["repeated-interruption"] == "bounded_recovery_within_retry_limit"
    assert harness.calls["cleanup"] >= 1
    rows = ResolveLease(harness.out / "lease.sqlite3", now=FakeClock()).holders()
    assert rows == []


def test_71_flow_verdict_fails_when_a_route_fails(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)

    def broken_build(_connection: ConnectionLike, interrupt_at: int | None) -> str:
        raise BuildInterrupted("always")

    object.__setattr__(harness.seams, "injection_build", broken_build)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=("partial-build",),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.verdict == "failed"
    assert "recovery_failed" in summary.failure_codes


def test_72_ab_evidence_records_structural_equality_and_presentation_diff(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=(),
        profiles=("a", "b"),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.profile_swap.passed
    assert summary.profile_swap.structural_equal
    assert summary.profile_swap.presentation_differences
    evidence_a, evidence_b = summary.profile_swap.profiles
    assert evidence_a.manifest_sha256 != evidence_b.manifest_sha256
    assert evidence_a.structure_sha256 == evidence_b.structure_sha256


def test_73_idempotent_rerun_revalidates_without_rerunning(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    extract = harness.extract
    binding = extract / "inputs/h1/binding.json"
    run_live_replay(
        extract=extract,
        h1_binding=binding,
        injections=("stale-state",),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    before = dict(harness.calls)
    revalidated = revalidate_live_replay(harness.out, seams=harness.seams)
    assert revalidated is not None
    assert revalidated.verdict == "passed"
    assert harness.calls == before


def test_74_rerun_runs_fresh_when_prior_summary_fails(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    extract = harness.extract
    binding = extract / "inputs/h1/binding.json"

    def always_abort(_connection: ConnectionLike, interrupt_at: int | None) -> str:
        raise BuildInterrupted("always")

    object.__setattr__(harness.seams, "injection_build", always_abort)
    broken = run_live_replay(
        extract=extract,
        h1_binding=binding,
        injections=("partial-build",),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    assert broken.verdict == "failed"
    assert revalidate_live_replay(harness.out, seams=harness.seams) is None

    def good_build(_connection: ConnectionLike, interrupt_at: int | None) -> str:
        if interrupt_at is not None:
            raise BuildInterrupted("planned abort")
        return "__fvp_test__rebuild"

    object.__setattr__(harness.seams, "injection_build", good_build)
    fresh = run_live_replay(
        extract=extract,
        h1_binding=binding,
        injections=("partial-build",),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    assert fresh.verdict == "passed"


def test_75_summary_model_is_strict_and_canonical(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=(),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    raw = (harness.out / "run-summary.json").read_bytes()
    assert LiveReplaySummary.model_validate_json(raw) == summary
    with pytest.raises(ValidationError, match="extra"):
        summary.model_validate_json(raw + b'{"extra": 1}')


def test_76_render_policy_declared_shape() -> None:
    policy = declared_render_policy()
    assert policy.container == "mp4"
    assert policy.video_codec == "h264"
    assert policy.audio_codec == "aac"
    assert (policy.width, policy.height) == (1920, 1080)
    assert policy.frame_rate == "30/1"
    assert policy.frame_count == 600
    assert policy.duration_ms == 20000
    assert policy.audio_channels == 2
    assert policy.audio_sample_rate_hz == 48000
    assert policy.audio_layout == "stereo"
