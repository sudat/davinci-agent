"""Blocker-fix regression: live replay revalidation is invocation-bound.

The old idempotent re-run validated only the prior render plus non-empty
evidence dirs, so a passing summary from ANY earlier invocation (other
extract, other H1 binding, other routes/profiles) was replayed as the
current success. The fixed flow persists a canonical invocation digest
(candidate id, git sha, H1 binding hash, routes, profiles, tool hashes)
plus hashed evidence files, and revalidation requires exact equality.
Diagnostic subsets can no longer emit the acceptance verdict either.
"""

from __future__ import annotations

from pathlib import Path

from services.release.live_flow import (
    LiveSeams,
    invocation_digest,
    revalidate_live_replay,
    run_live_replay,
)
from tests.release.test_live_replay import _flow_harness

ALL_ROUTES = (
    "partial-build",
    "resolve-restart",
    "stale-state",
    "false-success",
    "repeated-interruption",
)


def test_full_acceptance_invocation_passes_with_acceptance_scope(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=ALL_ROUTES,
        profiles=("a", "b"),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.verdict == "passed"
    assert summary.verdict_scope == "acceptance"
    assert summary.invocation is not None
    assert summary.invocation.injections == ALL_ROUTES
    assert summary.invocation.profiles == ("a", "b")
    assert summary.evidence_files


def test_diagnostic_subset_emits_distinct_diagnostic_verdict(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=("stale-state",),
        profiles=("a",),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.verdict == "diagnostic-passed"
    assert summary.verdict_scope == "diagnostic"
    assert summary.invocation is not None
    assert summary.invocation.injections == ("stale-state",)


def test_single_profile_full_routes_is_still_diagnostic(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    summary = run_live_replay(
        extract=harness.extract,
        h1_binding=harness.extract / "inputs/h1/binding.json",
        injections=ALL_ROUTES,
        profiles=("b",),
        out=harness.out,
        seams=harness.seams,
    )
    assert summary.verdict_scope == "diagnostic"
    assert summary.verdict != "passed"


def test_revalidation_requires_exact_invocation_match(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    extract = harness.extract
    binding = extract / "inputs/h1/binding.json"
    first = run_live_replay(
        extract=extract, h1_binding=binding, injections=("stale-state",),
        profiles=("a",), out=harness.out, seams=harness.seams,
    )
    assert first.invocation is not None
    assert first.invocation.invocation_digest == invocation_digest(first.invocation)

    same = revalidate_live_replay(
        harness.out, seams=harness.seams, invocation=first.invocation
    )
    assert same is not None
    assert same.invocation == first.invocation

    other_routes = first.invocation.model_copy(
        update={"injections": ("partial-build",)}
    )
    assert (
        revalidate_live_replay(
            harness.out, seams=harness.seams,
            invocation=other_routes.model_copy(
                update={"invocation_digest": invocation_digest(other_routes)}
            ),
        )
        is None
    )


def test_revalidation_detects_tampered_evidence_file(tmp_path: Path) -> None:
    harness = _flow_harness(tmp_path)
    extract = harness.extract
    binding = extract / "inputs/h1/binding.json"
    summary = run_live_replay(
        extract=extract, h1_binding=binding, injections=("stale-state",),
        profiles=("a",), out=harness.out, seams=harness.seams,
    )
    assert summary.invocation is not None
    target = next(
        Path(harness.out, row.path)
        for row in summary.evidence_files
        if Path(harness.out, row.path).is_file()
    )
    target.write_bytes(target.read_bytes() + b"tampered")
    assert (
        revalidate_live_replay(
            harness.out, seams=harness.seams, invocation=summary.invocation
        )
        is None
    )


def test_seams_type_still_constructible() -> None:
    assert LiveSeams.__dataclass_fields__ is not None
