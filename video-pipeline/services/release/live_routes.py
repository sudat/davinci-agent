"""Route dispatch for the live replay's pre-render injections (Todo 67).

Wraps each fault route with the flow's seams (scripted interrupt builds,
the restart seam with connection hand-off) so every injection outcome is
typed and honestly recorded; unexpected route failures are captured as
typed ``route_exception`` outcomes, never crashes or fake passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.job_runner.gate_p3_ab import compile_ab
from services.release.live_builds import ConnectionLike
from services.release.live_guard import LiveStateGuard
from services.release.staging import sha256_bytes

if TYPE_CHECKING:
    from services.release.live_flow import LiveSeams
from services.release.live_injections import (
    partial_build_route,
    repeated_interruption_route,
    resolve_restart_route,
    stale_state_route,
)
from services.release.live_models import (
    PROFILE_SNAPSHOT_IDS,
    InjectionOutcome,
    InjectionRoute,
    RestartObservation,
)


def run_injections(
    routes: tuple[InjectionRoute, ...],
    seams: LiveSeams,
    connection: dict[str, ConnectionLike],
    guard: LiveStateGuard,
    evidence_root: Path,
) -> list[InjectionOutcome]:
    outcomes: list[InjectionOutcome] = []
    current = guard.plan_sha256
    for route in routes:
        directory = evidence_root / route
        try:
            if route == "partial-build":
                outcome = partial_build_route(
                    interrupted=lambda: seams.injection_build(connection["conn"], 2),
                    clean=lambda: seams.injection_build(connection["conn"], None),
                    guard=guard,
                    evidence_dir=directory,
                    plan_sha256=guard.plan_sha256,
                )
            elif route == "resolve-restart":
                outcome = restart_route(seams, connection, guard, directory)
            elif route == "repeated-interruption":
                outcome = repeated_interruption_route(
                    attempts=(
                        lambda: seams.injection_build(connection["conn"], 1),
                        lambda: seams.injection_build(connection["conn"], 2),
                        lambda: seams.injection_build(connection["conn"], None),
                    ),
                    guard=guard,
                    evidence_dir=directory,
                    plan_sha256=guard.plan_sha256,
                )
            else:
                snap_a = PROFILE_SNAPSHOT_IDS["a"]
                manifest_a = compile_ab().manifests[snap_a]
                outcome = stale_state_route(
                    guard=guard,
                    evidence_dir=directory,
                    superseded_sha=current,
                    current_sha=sha256_bytes(canonical_model_bytes(manifest_a)),
                    unknown_sha=sha256_bytes(b"never-a-plan-sha"),
                )
        except Exception as error:  # noqa: BLE001 (recorded honestly, never faked)
            outcome = InjectionOutcome(
                route=route,
                outcome=f"route_exception:{type(error).__name__}",
                passed=False,
                detail=str(error)[:300],
                evidence_dir=str(directory),
            )
        outcomes.append(outcome)
    return outcomes


def restart_route(
    seams: LiveSeams,
    connection: dict[str, ConnectionLike],
    guard: LiveStateGuard,
    directory: Path,
) -> InjectionOutcome:
    def restart() -> tuple[RestartObservation, ConnectionLike]:
        observation, relaunched = seams.restart(connection["conn"])
        connection["conn"] = relaunched
        return observation, relaunched

    return resolve_restart_route(
        restart=restart,
        rebuild=lambda conn: seams.injection_build(conn, None),
        guard=guard,
        evidence_dir=directory,
        plan_sha256=guard.plan_sha256,
    )


__all__ = ["run_injections"]
