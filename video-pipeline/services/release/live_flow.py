"""Live fault-injecting replay orchestration (Todo 67 / F3).

Under an exclusive Resolve lease this flow runs the candidate's real
build/render path against live Resolve with honest fault injection, the
frozen A/B presentation profiles, a clean final render with measured
policy and anchor frame hashes, and a canonical run summary. Any route
that cannot prove its point makes the whole verdict fail — never faked.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.release.live_guard import (
    LEASE_HOLDER,
    LEASE_RESOURCE,
    LEASE_TTL_SECONDS,
    LiveStateGuard,
    ResolveLease,
    verify_h1_binding,
)
from services.release.live_injections import (
    false_success_route,
)
from services.release.live_media import anchor_indices, declared_render_policy, policy_matches

if TYPE_CHECKING:
    from services.release.live_builds import ConnectionLike

from services.release.live_models import (
    INJECTION_ROUTES,
    PROFILE_SNAPSHOT_IDS,
    AnchorFrame,
    AnchorPosition,
    EvidenceFileRef,
    FinalRenderObservation,
    InjectionOutcome,
    InjectionRoute,
    LeaseEvidence,
    LiveInvocation,
    LiveReplaySummary,
    ProfileBuildResult,
    ProfileId,
    ProfileSwapEvidence,
    RenderPolicy,
    RestartObservation,
)
from services.release.live_plan import frozen_ab_plan
from services.release.live_routes import run_injections
from services.release.live_swap import profile_swap
from services.release.manifest import candidate_id
from services.release.staging import sha256_bytes
from services.release.verify import read_manifest

SUMMARY_NAME = "run-summary.json"
FINAL_RENDER_NAME = "final.mp4"
MAX_EVIDENCE_FILES = 512
ACCEPTANCE_PROFILES: tuple[ProfileId, ...] = ("a", "b")
PRE_RENDER_ROUTES: tuple[InjectionRoute, ...] = (
    "partial-build",
    "resolve-restart",
    "repeated-interruption",
    "stale-state",
)


def invocation_digest(invocation: LiveInvocation) -> str:
    """sha256 over the canonical invocation identity (digest zeroed)."""
    payload = invocation.model_copy(update={"invocation_digest": "0" * 64})
    return sha256_bytes(canonical_model_bytes(payload))


def complete_invocation(
    *,
    candidate_id: str,
    git_sha: str,
    h1_binding_sha256: str,
    injections: tuple[InjectionRoute, ...],
    profiles: tuple[ProfileId, ...],
    tool_sha256: tuple[str, ...] = (),
) -> LiveInvocation:
    unsealed = LiveInvocation(
        candidate_id=candidate_id,
        extract_git_sha=git_sha,
        h1_binding_sha256=h1_binding_sha256,
        injections=injections,
        profiles=profiles,
        tool_sha256=tool_sha256,
        invocation_digest="0" * 64,
    )
    return unsealed.model_copy(update={"invocation_digest": invocation_digest(unsealed)})


def _acceptance_coverage(
    injections: tuple[InjectionRoute, ...], profiles: tuple[ProfileId, ...]
) -> bool:
    return tuple(injections) == INJECTION_ROUTES and tuple(profiles) == ACCEPTANCE_PROFILES


def parse_injections(raw: str) -> tuple[InjectionRoute, ...]:
    routes: list[InjectionRoute] = []
    for token in raw.split(","):
        stripped = token.strip()
        if stripped not in INJECTION_ROUTES:
            raise ValueError(f"unknown injection route: {stripped}")
        if stripped not in routes:
            routes.append(cast("InjectionRoute", stripped))
    if not routes:
        raise ValueError("at least one injection route is required")
    return tuple(routes)


def parse_profiles(raw: str) -> tuple[ProfileId, ...]:
    profiles: list[ProfileId] = []
    for token in raw.split(","):
        stripped = token.strip()
        if stripped not in PROFILE_SNAPSHOT_IDS or stripped in profiles:
            raise ValueError(f"profile must be a unique a/b selector: {token}")
        profiles.append(cast("ProfileId", stripped))
    if not profiles:
        raise ValueError("at least one profile is required")
    return tuple(profiles)


@dataclass(frozen=True)
class LiveSeams:
    """Every live surface the flow touches, injectable for offline fakes."""

    connect: Callable[[], ConnectionLike]
    restart: Callable[[ConnectionLike], tuple[RestartObservation, ConnectionLike]]
    injection_build: Callable[[ConnectionLike, int | None], str]
    profile_build: Callable[[ConnectionLike, str, Path], ProfileBuildResult]
    measure: Callable[[Path], RenderPolicy]
    anchors: Callable[[Path, Path, Mapping[AnchorPosition, int]], tuple[AnchorFrame, ...]]
    anchor_argv: Callable[[Path, Path, Mapping[AnchorPosition, int]], tuple[str, ...]]
    now: Callable[[], int]
    cleanup: Callable[[ConnectionLike], tuple[str, ...]]


def run_live_replay(  # noqa: PLR0912 (the flow enumerates every honest failure path)
    *,
    extract: Path,
    h1_binding: Path,
    injections: tuple[InjectionRoute, ...],
    profiles: tuple[ProfileId, ...],
    out: Path,
    seams: LiveSeams,
    tool_sha256: tuple[str, ...] = (),
) -> LiveReplaySummary:
    """Drive the live replay end to end; the summary is always honest."""

    h1 = verify_h1_binding(extract, h1_binding)
    manifest, _ = read_manifest(extract)
    candidate = candidate_id(manifest)
    git_sha = h1.git_sha
    invocation = complete_invocation(
        candidate_id=candidate,
        git_sha=git_sha,
        h1_binding_sha256=h1.binding_sha256,
        injections=injections,
        profiles=profiles,
        tool_sha256=tool_sha256,
    )
    out.mkdir(parents=True, exist_ok=True)
    for name in ("evidence", "render"):
        shutil.rmtree(out / name, ignore_errors=True)
    lease = ResolveLease(
        out / "lease.sqlite3", now=seams.now, resource=LEASE_RESOURCE, holder=LEASE_HOLDER,
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    lease_evidence = LeaseEvidence(
        db_path=str(lease.db_path),
        resource=LEASE_RESOURCE,
        holder=LEASE_HOLDER,
        ttl_seconds=LEASE_TTL_SECONDS,
        acquired=False,
        released=False,
    )
    outcomes: list[InjectionOutcome] = []
    swap = ProfileSwapEvidence(
        profiles=(), structural_equal=True, presentation_differences=(), passed=False
    )
    final_observation: FinalRenderObservation | None = None
    failures: list[str] = []
    final_render_source: Path | None = None
    connection: dict[str, ConnectionLike] = {}
    lease.acquire()
    lease_evidence = lease_evidence.model_copy(update={"acquired": True})
    try:
        connection["conn"] = seams.connect()
        pre: tuple[InjectionRoute, ...] = tuple(
            route for route in PRE_RENDER_ROUTES if route in injections
        )
        outcomes.extend(
            run_injections(pre, seams, connection, LiveStateGuard(_plan_sha()), out / "evidence")
        )
        swap, final_render_source, swap_failures = profile_swap(
            seams, connection, profiles, out / "evidence"
        )
        failures.extend(swap_failures)
        if final_render_source is not None:
            final_observation = _finalize_render(seams, out, final_render_source, profiles[-1])
            if not final_observation.policy_verified:
                failures.append("render_policy_mismatch")
        else:
            failures.append("final_render_missing")
        if "false-success" in injections and final_render_source is not None:
            outcomes.append(
                false_success_route(
                    render_path=out / "render" / FINAL_RENDER_NAME,
                    declared_policy=declared_render_policy(),
                    measure=seams.measure,
                    evidence_dir=out / "evidence" / "false-success",
                )
            )
        elif "false-success" in injections:
            outcomes.append(
                InjectionOutcome(
                    route="false-success",
                    outcome="final_render_unavailable",
                    passed=False,
                    detail="cannot verify claims without a real final render",
                    evidence_dir=str(out / "evidence" / "false-success"),
                )
            )
    except Exception as error:  # noqa: BLE001 (typed honest failure, never a crash pass)
        failures.append(f"flow_exception:{type(error).__name__}:{error}"[:300])
    finally:
        if connection.get("conn") is not None:
            try:
                seams.cleanup(connection["conn"])
            except Exception as error:  # noqa: BLE001 (cleanup must not mask results)
                failures.append(f"cleanup_failed:{type(error).__name__}")
        lease.release()
        lease_evidence = lease_evidence.model_copy(update={"released": True})
    failures.extend(row.outcome for row in outcomes if not row.passed)
    if not swap.passed and "profile_swap_failed" not in failures:
        failures.append("profile_swap_failed")
    scope: Literal["acceptance", "diagnostic"] = (
        "acceptance" if _acceptance_coverage(injections, profiles) else "diagnostic"
    )
    if not failures and scope == "acceptance":
        verdict: Literal["passed", "failed", "diagnostic-passed"] = "passed"
    elif not failures:
        verdict = "diagnostic-passed"
    else:
        verdict = "failed"
    evidence_files, evidence_complete = _hash_evidence_files(out)
    summary = LiveReplaySummary(
        extract_path=str(extract),
        extract_git_sha=git_sha,
        candidate_id=candidate,
        h1=h1,
        lease=lease_evidence,
        injections=tuple(outcomes),
        profile_swap=swap,
        final_render=final_observation
        if final_observation is not None
        else _missing_final_observation(),
        verdict=verdict,
        verdict_scope=scope,
        invocation=invocation,
        evidence_files=evidence_files,
        evidence_files_complete=evidence_complete,
        failure_codes=tuple(dict.fromkeys(failures)),
    )
    atomic_write(out / SUMMARY_NAME, canonical_model_bytes(summary))
    return summary


def _hash_evidence_files(out: Path) -> tuple[tuple[EvidenceFileRef, ...], bool]:
    """Hash-bind the bounded evidence tree for revalidation."""
    evidence_root = out / "evidence"
    if not evidence_root.is_dir():
        return (), True
    files: list[EvidenceFileRef] = []
    complete = True
    stack = [evidence_root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if entry.is_dir():
                stack.append(entry)
                continue
            if len(files) >= MAX_EVIDENCE_FILES:
                complete = False
                continue
            files.append(
                EvidenceFileRef(
                    path=entry.relative_to(out).as_posix(),
                    sha256=sha256_file(entry),
                )
            )
    return tuple(files), complete


def _plan_sha() -> str:
    return sha256_bytes(canonical_model_bytes(frozen_ab_plan().timeline_ir))


def _finalize_render(
    seams: LiveSeams, out: Path, source: Path, profile: ProfileId
) -> FinalRenderObservation:
    render_dir = out / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    final = render_dir / FINAL_RENDER_NAME
    shutil.copyfile(source, final)
    declared = declared_render_policy()
    measured = seams.measure(final)
    count = measured.frame_count if measured.frame_count > 0 else declared.frame_count
    indices = anchor_indices(count)
    anchors = seams.anchors(final, render_dir / "anchors", indices)
    return FinalRenderObservation(
        path=f"render/{FINAL_RENDER_NAME}",
        sha256=sha256_file(final),
        source_profile=profile,
        anchors=anchors,
        anchor_extraction_argv=seams.anchor_argv(
            final, render_dir / "anchors", indices
        ),
        policy_declared=declared,
        policy_measured=measured,
        policy_verified=policy_matches(declared, measured),
    )


def _missing_final_observation() -> FinalRenderObservation:
    return FinalRenderObservation(
        path="",
        sha256=sha256_bytes(b"missing"),
        source_profile="a",
        anchors=(),
        anchor_extraction_argv=(),
        policy_declared=declared_render_policy(),
        policy_measured=declared_render_policy(),
        policy_verified=False,
    )


def revalidate_live_replay(
    out: Path,
    *,
    seams: LiveSeams,
    invocation: LiveInvocation,
) -> LiveReplaySummary | None:
    """Idempotent re-run: the prior summary must match THIS invocation.

    The persisted summary is only accepted when its invocation record
    equals the current one EXACTLY (candidate, git sha, H1 binding hash,
    routes, profiles, tool hashes, digest), every hash-bound evidence
    file still re-hashes to its recorded value, and the render still
    re-verifies. Anything else forces a fresh run.
    """

    path = out / SUMMARY_NAME
    if not path.is_file():
        return None
    summary = LiveReplaySummary.model_validate_json(path.read_bytes())
    if summary.verdict not in ("passed", "diagnostic-passed"):
        return None
    if summary.invocation is None or summary.invocation != invocation:
        return None
    if summary.invocation.invocation_digest != invocation_digest(summary.invocation):
        return None
    for row in summary.evidence_files:
        evidence_file = out / row.path
        if not evidence_file.is_file() or sha256_file(evidence_file) != row.sha256:
            return None
    if not _final_render_reverifies(out, summary, seams):
        return None
    for row in summary.injections:
        evidence_dir = Path(row.evidence_dir)
        if not evidence_dir.is_dir() or not any(evidence_dir.iterdir()):
            return None
    return summary


def _final_render_reverifies(
    out: Path, summary: LiveReplaySummary, seams: LiveSeams
) -> bool:
    final = out / summary.final_render.path
    if not final.is_file() or sha256_file(final) != summary.final_render.sha256:
        return False
    if seams.measure(final) != summary.final_render.policy_measured:
        return False
    indices = anchor_indices(summary.final_render.policy_measured.frame_count)
    recomputed = seams.anchors(final, out / "render" / "revalidate-anchors", indices)
    return recomputed == tuple(summary.final_render.anchors)


__all__ = [
    "FINAL_RENDER_NAME",
    "SUMMARY_NAME",
    "LiveSeams",
    "complete_invocation",
    "invocation_digest",
    "parse_injections",
    "parse_profiles",
    "revalidate_live_replay",
    "run_live_replay",
]
