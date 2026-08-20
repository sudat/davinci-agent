"""Attack class 3: path/symlink traversal out of managed roots — denied.

Active attempts against the REAL guards: the policy path-guard, the
retention no-follow readers/walkers, and the retention GC executor. Every
attempt asserts the typed denial AND zero side effects on the victim files
outside the managed root (pre/post snapshot diff), proving an escape can
never delete or mutate anything outside the root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.config.models import PathAllowlist
from services.policy.path_guard import PathPolicyError, resolve_path
from services.retention.errors import RetentionError
from services.retention.gc import RetentionGc
from services.retention.policy import load_policy
from services.retention.walk import load_job_state, walk_tree
from tests.security.support import assert_zero_side_effects, snapshot_tree

VICTIM_CONTENT = b"todo66-victim-secret"


def _victim(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_bytes(VICTIM_CONTENT)
    return victim


def test_10_dotdot_traversal_denied_with_victim_untouched(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    before = snapshot_tree(tmp_path)

    with pytest.raises(PathPolicyError, match="traversal"):
        resolve_path(str(jobs) + "/../outside/victim.txt", PathAllowlist(roots=(str(jobs),)))

    assert victim.read_bytes() == VICTIM_CONTENT
    assert_zero_side_effects(tmp_path, before)


def test_11_absolute_escape_denied(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    before = snapshot_tree(tmp_path)
    with pytest.raises(PathPolicyError):
        resolve_path(str(victim), PathAllowlist(roots=(str(jobs),)))
    assert_zero_side_effects(tmp_path, before)


def test_12_symlink_escape_denied(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    jobs = tmp_path / "jobs"
    (jobs / "ep-1").mkdir(parents=True)
    (jobs / "ep-1" / "link").symlink_to(victim.parent)
    before = snapshot_tree(tmp_path)
    with pytest.raises(PathPolicyError, match="allowlist"):
        resolve_path(jobs / "ep-1" / "link" / "victim.txt", PathAllowlist(roots=(str(jobs),)))
    assert victim.read_bytes() == VICTIM_CONTENT
    assert_zero_side_effects(tmp_path, before)


def test_13_malformed_paths_are_typed_not_crashes(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    allow = PathAllowlist(roots=(str(jobs),))
    for bad in ("", "relative/x"):
        with pytest.raises(PathPolicyError):
            resolve_path(bad, allow)


def _policy_file(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "retention-policy-v1",
        "rebuildable_retention_days": 30,
        "authoritative_names": ("sources", "artifacts", "renders"),
        "manual_finalization_names": ("manual-finalization",),
        "rebuildable_names": ("proxies", "previews", "analysis", "contacts"),
        "runtime_cache_names": ("cache",),
    }
    path = tmp_path / "retention-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _frozen_job(jobs: Path, job_id: str = "job-a") -> Path:
    job = jobs / job_id
    (job / "sources").mkdir(parents=True)
    (job / "sources" / "cam.mov").write_bytes(b"cam-original")
    (job / "proxies").mkdir()
    (job / "proxies" / "p1.mp4").write_bytes(b"proxy-bytes")
    state = {
        "schema_version": "retention-job-state-v1",
        "job_id": job_id,
        "status": "FROZEN",
        "frozen_at_epoch_s": 1_000_000_000,
    }
    (job / "job-state.json").write_text(json.dumps(state), encoding="utf-8")
    return job


def test_20_symlinked_job_state_refused(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    jobs = tmp_path / "jobs"
    job = _frozen_job(jobs)
    honest = (job / "job-state.json").read_bytes()
    (job / "job-state.json").unlink()
    (job / "job-state.json").symlink_to(victim)
    with pytest.raises(RetentionError, match="job-state-invalid"):
        load_job_state(job)
    assert victim.read_bytes() == VICTIM_CONTENT  # never read through the link
    (job / "job-state.json").unlink()
    (job / "job-state.json").write_bytes(honest)


def test_21_walk_reports_symlinks_without_following(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    jobs = tmp_path / "jobs"
    job = _frozen_job(jobs)
    (job / "proxies" / "escape").symlink_to(victim.parent)
    root_real = os.path.realpath(jobs)
    entries = walk_tree(job, root_real)
    symlinks = {e.relative: e.symlink_target for e in entries if e.kind == "symlink"}
    assert "proxies/escape" in symlinks
    assert victim.read_bytes() == VICTIM_CONTENT


def test_22_gc_never_deletes_through_a_symlink_escape(tmp_path: Path) -> None:
    """The headline traversal attack: symlinked rebuildable dir at GC time.

    ``proxies`` is replaced by a symlink to the victim directory; the GC
    must refuse/retain it — the victim file and every outside byte must be
    identical after a full execute-mode run (pre/post snapshot diff).
    """

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_bytes(VICTIM_CONTENT)
    jobs = tmp_path / "jobs"
    job = _frozen_job(jobs)
    (job / "proxies" / "escape-link").symlink_to(outside)
    before_outside = snapshot_tree(outside)
    gc = RetentionGc(load_policy(_policy_file(tmp_path)))
    outcome = gc.run(jobs, "execute")
    deleted = {d.path for d in outcome.plan.decisions if d.action == "deleted"}
    for path in deleted:
        assert not path.startswith(("/", ".."))
        assert "escape-link" not in path
    assert victim.read_bytes() == VICTIM_CONTENT
    assert_zero_side_effects(outside, before_outside)
