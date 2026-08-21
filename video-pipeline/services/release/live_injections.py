"""The five live fault-injection routes (Todo 67 / F3).

Each route honestly exercises one failure mode against the real flow and
records a typed outcome: a route passes only when the SYSTEM behaved as
declared (typed interruption, typed rejection, detection by recompute,
bounded recovery). A route that cannot prove its point fails honestly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from services.foundation_io import sha256_file
from services.release.live_builds import (
    BuildInterrupted,
    ConnectionLike,
    RetryLimiter,
    RetryLimitError,
)
from services.release.live_guard import LiveStateGuard, StaleStateError
from services.release.live_media import RenderPolicy, verify_render_claim
from services.release.live_models import InjectionOutcome, InjectionRoute, RestartObservation

BuildAttempt = Callable[[], str]
MIN_ROUTE_ABORTS = 2


def _record(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _outcome(
    route: InjectionRoute,
    evidence_dir: Path,
    outcome: str,
    *,
    passed: bool,
    detail: str,
) -> InjectionOutcome:
    return InjectionOutcome(
        route=route,
        outcome=outcome,
        passed=passed,
        detail=detail,
        evidence_dir=str(evidence_dir),
    )


def partial_build_route(
    *,
    interrupted: BuildAttempt,
    clean: BuildAttempt,
    guard: LiveStateGuard,
    evidence_dir: Path,
    plan_sha256: str,
) -> InjectionOutcome:
    """Abort a build partway; recover only via a clean rebuild commit."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        interrupted()
    except BuildInterrupted as error:
        guard.commit(
            evidence_dir, "aborted-record.json", _record({"interrupted": str(error)}),
            plan_sha256=plan_sha256,
        )
    else:
        return _outcome(
            "partial-build",
            evidence_dir,
            "interrupt_not_observed",
            passed=False,
            detail="the declared kill seam did not interrupt the build",
        )
    committed = evidence_dir / "committed"
    half_state = committed.is_dir() and list(committed.glob("*.json"))
    if half_state:
        return _outcome(
            "partial-build",
            evidence_dir,
            "half_state_committed",
            passed=False,
            detail=f"aborted build left committed artifacts: {sorted(p.name for p in half_state)}",
        )
    try:
        project = clean()
    except BuildInterrupted as error:
        return _outcome(
            "partial-build",
            evidence_dir,
            "recovery_failed",
            passed=False,
            detail=f"clean rebuild was interrupted too: {error}",
        )
    guard.commit(
        committed, f"build-{project}.json", _record({"project": project, "recovered": True}),
        plan_sha256=plan_sha256,
    )
    committed_files = sorted(path.name for path in committed.glob("*.json"))
    passed = committed_files == [f"build-{project}.json"]
    return _outcome(
        "partial-build",
        evidence_dir,
        "recovered_clean_rebuild" if passed else "half_state_committed",
        passed=passed,
        detail=f"aborted once, rebuilt clean; committed={committed_files}",
    )


def repeated_interruption_route(
    *,
    attempts: tuple[BuildAttempt, ...],
    guard: LiveStateGuard,
    evidence_dir: Path,
    plan_sha256: str,
    max_attempts: int = 3,
) -> InjectionOutcome:
    """Interrupt the same build repeatedly; recovery must stay bounded."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    limiter = RetryLimiter(max_attempts=max_attempts)
    aborts = 0
    recovered_on = 0
    project = ""
    for index, attempt in enumerate(attempts, start=1):
        limiter.require_can_attempt()
        limiter.record_attempt()
        try:
            project = attempt()
        except BuildInterrupted:
            aborts += 1
            continue
        recovered_on = index
        break
    try:
        limiter.require_can_attempt()
        retry_limit_enforced = False
    except RetryLimitError:
        retry_limit_enforced = True
    if recovered_on:
        guard.commit(
            evidence_dir, "repeated-interruption.json",
            _record(
                {
                    "aborts": aborts,
                    "recovered_on_attempt": recovered_on,
                    "retry_limit_enforced": retry_limit_enforced,
                    "max_attempts": max_attempts,
                }
            ),
            plan_sha256=plan_sha256,
        )
        guard.commit(
            evidence_dir / "committed", f"build-{project}.json",
            _record({"project": project, "recovered": True, "aborts": aborts}),
            plan_sha256=plan_sha256,
        )
        passed = retry_limit_enforced and aborts >= MIN_ROUTE_ABORTS
        return _outcome(
            "repeated-interruption",
            evidence_dir,
            "bounded_recovery_within_retry_limit" if passed else "insufficient_interruptions",
            passed=passed,
            detail=(
                f"aborts={aborts} recovered_on={recovered_on} "
                f"limit_enforced={retry_limit_enforced}"
            ),
        )
    outcome = "recovery_exhausted_retry_limit" if retry_limit_enforced else "attempts_incomplete"
    guard.commit(
        evidence_dir, "repeated-interruption.json",
        _record({"aborts": aborts, "recovered_on_attempt": 0, "max_attempts": max_attempts}),
        plan_sha256=plan_sha256,
    )
    return _outcome(
        "repeated-interruption",
        evidence_dir,
        outcome,
        passed=False,
        detail=f"no clean recovery within {max_attempts} attempts (aborts={aborts})",
    )


def stale_state_route(
    *,
    guard: LiveStateGuard,
    evidence_dir: Path,
    superseded_sha: str,
    current_sha: str,
    unknown_sha: str,
) -> InjectionOutcome:
    """Present superseded/unknown state; only typed rejection may result."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    guard.require(superseded_sha)
    guard.rotate(current_sha)
    codes: list[str] = []
    for name, sha in (("stale-artifact.json", superseded_sha), ("unknown.json", unknown_sha)):
        try:
            guard.commit(evidence_dir, name, _record({"plan": sha}), plan_sha256=sha)
        except StaleStateError as error:
            codes.append(error.code)
        else:
            codes.append("ACCEPTED")
    silently_used = [
        name
        for name in ("stale-artifact.json", "unknown.json")
        if (evidence_dir / name).exists()
    ]
    guard.commit(
        evidence_dir, "current-artifact.json", _record({"plan": current_sha}),
        plan_sha256=current_sha,
    )
    passed = (
        codes == ["superseded_plan_sha", "stale_plan_sha"]
        and not silently_used
        and (evidence_dir / "current-artifact.json").is_file()
    )
    return _outcome(
        "stale-state",
        evidence_dir,
        "typed_rejection_no_silent_use" if passed else "silent_stale_use_detected",
        passed=passed,
        detail=f"codes={codes} silently_used={silently_used}",
    )


def false_success_route(
    *,
    render_path: Path,
    declared_policy: RenderPolicy,
    measure: Callable[[Path], RenderPolicy],
    evidence_dir: Path,
) -> InjectionOutcome:
    """Fake success claims must be caught by hash/policy recompute."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    true_sha = sha256_file(render_path)
    tampered = evidence_dir / "tampered-render.mp4"
    raw = bytearray(render_path.read_bytes())
    middle = len(raw) // 2
    raw[middle] = raw[middle] ^ 0x01
    tampered.write_bytes(bytes(raw))
    hash_tamper = verify_render_claim(
        tampered, declared_sha256=true_sha, declared_policy=declared_policy, measure=measure
    )
    lying = declared_policy.model_copy(
        update={"frame_count": declared_policy.frame_count + 999}
    )
    policy_lie = verify_render_claim(
        render_path, declared_sha256=true_sha, declared_policy=lying, measure=measure
    )
    honest = verify_render_claim(
        render_path, declared_sha256=true_sha, declared_policy=declared_policy, measure=measure
    )
    detected = (
        hash_tamper.outcome == "claim_hash_mismatch"
        and policy_lie.outcome == "claim_policy_mismatch"
        and honest.outcome == "verified"
    )
    record = _record(
        {
            "hash_tamper": hash_tamper.outcome,
            "policy_lie": policy_lie.outcome,
            "honest_control": honest.outcome,
            "declared_frame_count": declared_policy.frame_count,
        }
    )
    (evidence_dir / "false-success.json").write_bytes(record)
    return _outcome(
        "false-success",
        evidence_dir,
        "tamper_detected_by_recompute" if detected else "false_claim_accepted",
        passed=detected,
        detail=f"hash={hash_tamper.outcome} policy={policy_lie.outcome} control={honest.outcome}",
    )


def resolve_restart_route(
    *,
    restart: Callable[[], tuple[RestartObservation, ConnectionLike]],
    rebuild: Callable[[ConnectionLike], str],
    guard: LiveStateGuard,
    evidence_dir: Path,
    plan_sha256: str,
) -> InjectionOutcome:
    """Restart Resolve mid-flow; reconnect must be bounded and resumed."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    observation, new_connection = restart()
    if not observation.down_probe_error:
        return _outcome(
            "resolve-restart",
            evidence_dir,
            "down_probe_succeeded",
            passed=False,
            detail="expected a typed bridge failure while Resolve was down",
        )
    if not observation.binding_same:
        return _outcome(
            "resolve-restart",
            evidence_dir,
            "binding_drift_after_restart",
            passed=False,
            detail="relaunched Resolve reports a different version binding",
        )
    try:
        project = rebuild(new_connection)
    except BuildInterrupted as error:
        return _outcome(
            "resolve-restart",
            evidence_dir,
            "resume_rebuild_failed",
            passed=False,
            detail=f"post-restart rebuild interrupted: {error}",
        )
    guard.commit(
        evidence_dir / "committed", f"build-{project}.json",
        _record(
            {
                "project": project,
                "quit_method": observation.quit_method,
                "relaunch_seconds": observation.relaunch_seconds,
            }
        ),
        plan_sha256=plan_sha256,
    )
    return _outcome(
        "resolve-restart",
        evidence_dir,
        "bounded_reconnect_resumed",
        passed=True,
        detail=(
            f"typed-down={observation.down_probe_error.split(':')[0]} "
            f"relaunch_seconds={observation.relaunch_seconds}"
        ),
    )


__all__ = [
    "false_success_route",
    "partial_build_route",
    "repeated_interruption_route",
    "resolve_restart_route",
    "stale_state_route",
]
