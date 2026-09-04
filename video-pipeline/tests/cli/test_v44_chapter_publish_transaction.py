"""Publication transaction: rollback-safe master/evidence/QA set (TDD)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import services.cli._v44_chapter_card_run as run_module
from services.cli._v44_chapter_card_gates import ChapterCardInsertError
from services.cli._v44_chapter_card_run import EVIDENCE_NAME, MASTER_NAME
from services.foundation_io import sha256_file
from tests.cli.v44_chapter_scaled_episode import (
    World,
    make_scaled_world,
    run_world,
    run_world_once,
    snapshot,
)

QA_NAME = "qa"


@pytest.fixture(scope="module")
def scaled_world(tmp_path_factory: pytest.TempPathFactory) -> World:
    return make_scaled_world(tmp_path_factory.mktemp("chapter-card-transaction"))


def _fail_first_replace(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, always: bool = False
) -> None:
    """Make os.replace raise for `target` as destination (first time, or always)."""

    real_replace = os.replace
    fired = False

    def _patched(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal fired
        if Path(dst) == target and (always or not fired):
            fired = True
            raise OSError(f"injected replace failure for {target.name}")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _patched)


def _dotfiles(output_dir: Path) -> list[str]:
    return sorted(entry.name for entry in output_dir.iterdir() if entry.name.startswith("."))


def test_evidence_publish_failure_restores_old_master_and_evidence(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a previously published artifact set (master, evidence, and QA)
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    old_master = master.read_bytes()
    old_evidence = evidence.read_bytes()
    old_qa = snapshot(output_dir / QA_NAME)
    tools, _, _, _ = scaled_world

    # When: a rerun's evidence replacement fails after the master was replaced
    _fail_first_replace(monkeypatch, evidence)
    with pytest.raises(ChapterCardInsertError, match="fs-replace-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: the old master, evidence, and QA set are byte-identical, no leftovers
    assert master.read_bytes() == old_master
    assert evidence.read_bytes() == old_evidence
    assert snapshot(output_dir / QA_NAME) == old_qa
    assert _dotfiles(output_dir) == []


def test_qa_publish_failure_restores_the_entire_prior_set(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a previously published master/evidence/QA set
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    old_set = {
        "master": master.read_bytes(),
        "evidence": evidence.read_bytes(),
        "qa": snapshot(output_dir / QA_NAME),
    }
    tools, _, _, _ = scaled_world

    # When: the QA directory rename fails after master and evidence were replaced
    _fail_first_replace(monkeypatch, output_dir / QA_NAME)
    with pytest.raises(ChapterCardInsertError, match="fs-replace-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: the entire prior set is restored exactly, with no leftovers
    assert master.read_bytes() == old_set["master"]
    assert evidence.read_bytes() == old_set["evidence"]
    assert snapshot(output_dir / QA_NAME) == old_set["qa"]
    assert _dotfiles(output_dir) == []


def test_successful_publish_updates_all_three_coherently(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one published set followed by a second successful run
    tools, _, _, _ = scaled_world
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    first_qa = snapshot(output_dir / QA_NAME)

    # When: the rerun publishes
    run_world(tools, scaled_world, output_dir)

    # Then: master/evidence/QA are all refreshed, consistent, and no temp/backup remains
    assert {entry.name for entry in output_dir.iterdir()} == {
        EVIDENCE_NAME, MASTER_NAME, QA_NAME, "work",
    }
    assert _dotfiles(output_dir) == []
    assert snapshot(output_dir / QA_NAME).keys() == first_qa.keys()
    document = json.loads(evidence.read_bytes())
    assert document["output"]["sha256"] == sha256_file(master)


def _fail_backup_rename(
    monkeypatch: pytest.MonkeyPatch, final: Path, *, restore_target: Path | None = None
) -> None:
    """Fail the backup rename (final -> .v44-backup-*) for `final`.

    When ``restore_target`` is set, restoring that artifact's backup fails too,
    forcing the rollback-failure path while `final`'s reservation never completed.
    """

    real_replace = os.replace
    backup_failed = False

    def _patched(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal backup_failed
        source, target = Path(src), Path(dst)
        if source == final and target.name.startswith(".v44-backup-") and not backup_failed:
            backup_failed = True
            raise OSError(f"injected backup failure for {final.name}")
        if restore_target is not None and target == restore_target:
            raise OSError(f"injected restore failure for {restore_target.name}")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _patched)


def test_master_backup_failure_leaves_every_prior_artifact_untouched(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published master/evidence/QA set
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    prior = {
        "master": master.read_bytes(),
        "evidence": evidence.read_bytes(),
        "qa": snapshot(output_dir / QA_NAME),
    }
    tools, _, _, _ = scaled_world

    # When: the very first backup rename (master -> backup) fails
    _fail_backup_rename(monkeypatch, master)
    with pytest.raises(ChapterCardInsertError, match="fs-replace-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: every prior artifact is byte-identical and no placeholder remains
    assert master.read_bytes() == prior["master"]
    assert evidence.read_bytes() == prior["evidence"]
    assert snapshot(output_dir / QA_NAME) == prior["qa"]
    assert _dotfiles(output_dir) == []


def test_evidence_backup_failure_never_restores_an_empty_placeholder(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published set where the master backup succeeds first
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    prior = {
        "master": master.read_bytes(),
        "evidence": evidence.read_bytes(),
        "qa": snapshot(output_dir / QA_NAME),
    }
    assert prior["evidence"], "the prior evidence must be real bytes"
    tools, _, _, _ = scaled_world

    # When: the evidence backup rename fails after the master backup completed
    _fail_backup_rename(monkeypatch, evidence)
    with pytest.raises(ChapterCardInsertError, match="fs-replace-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: the master is restored from its completed backup, the evidence is
    # byte-identical (never overwritten by the empty reserved placeholder),
    # the QA set is unchanged, and no placeholder lingers
    assert master.read_bytes() == prior["master"]
    assert evidence.read_bytes() == prior["evidence"]
    assert snapshot(output_dir / QA_NAME) == prior["qa"]
    assert _dotfiles(output_dir) == []


def test_qa_backup_failure_restores_both_files(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published set where master and evidence backups succeed first
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    prior = {
        "master": master.read_bytes(),
        "evidence": evidence.read_bytes(),
        "qa": snapshot(output_dir / QA_NAME),
    }
    tools, _, _, _ = scaled_world

    # When: the QA directory backup rename fails last
    _fail_backup_rename(monkeypatch, output_dir / QA_NAME)
    with pytest.raises(ChapterCardInsertError, match="fs-replace-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: both files are restored and the QA set is byte-identical
    assert master.read_bytes() == prior["master"]
    assert evidence.read_bytes() == prior["evidence"]
    assert snapshot(output_dir / QA_NAME) == prior["qa"]
    assert _dotfiles(output_dir) == []


def test_rollback_failure_advertises_only_completed_backups(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published set where the evidence backup rename fails AND the
    # master backup restore fails afterwards
    master, _, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    prior_master = master.read_bytes()
    tools, _, _, _ = scaled_world
    _fail_backup_rename(
        monkeypatch, output_dir / EVIDENCE_NAME, restore_target=master
    )

    # When: publication and master restoration both fail
    with pytest.raises(ChapterCardInsertError, match="publish-rollback-failed") as caught:
        run_world(tools, scaled_world, output_dir)

    # Then: the preserved-backup list names only the completed master backup,
    # never the empty evidence reservation (which must be deleted)
    message = str(caught.value)
    assert ".v44-backup-" in message
    assert EVIDENCE_NAME not in message
    leftovers = [entry for entry in output_dir.iterdir() if entry.name.startswith(".v44-backup")]
    assert len(leftovers) == 1, "exactly the completed master backup remains"
    assert leftovers[0].read_bytes() == prior_master


def test_rollback_failure_preserves_backups_and_raises_distinct_code(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published set and a rerun whose evidence replace fails, with the
    # backup restore for the evidence ALSO failing afterwards
    master, evidence, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    old_master = master.read_bytes()
    tools, _, _, _ = scaled_world
    _fail_first_replace(monkeypatch, evidence, always=True)

    # When: publication and restoration both fail
    with pytest.raises(ChapterCardInsertError, match="publish-rollback-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: a distinct typed error names the preserved backups, which still exist,
    # and every recoverable prior artifact (the master) keeps its exact bytes
    backups = [entry for entry in output_dir.iterdir() if entry.name.startswith(".v44-backup")]
    assert backups, "backups must be preserved when restoration fails"
    assert master.read_bytes() == old_master
    assert _dotfiles(output_dir) == sorted(entry.name for entry in backups)


def test_failed_run_never_mutates_published_qa(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a published set whose QA bytes carry a distinctive stale marker
    master, _, output_dir = run_world_once(tmp_path, scaled_world, monkeypatch)
    marker_png = next(iter(sorted((output_dir / QA_NAME).glob("*.png"))))
    marker_png.write_bytes(b"stale-published-qa-marker")
    old_qa = snapshot(output_dir / QA_NAME)
    old_master = master.read_bytes()
    tools, _, _, _ = scaled_world

    # When: a rerun passes visual QA (staging its PNGs) but fails the audio proof
    def _audio_refusal(*args: object, **kwargs: object) -> dict[str, object]:
        raise ChapterCardInsertError("audio-identity-failed", "injected")

    monkeypatch.setattr(run_module, "audio_proof", _audio_refusal)
    with pytest.raises(ChapterCardInsertError, match="audio-identity-failed"):
        run_world(tools, scaled_world, output_dir)

    # Then: the published QA set and master are untouched and staging is gone
    assert snapshot(output_dir / QA_NAME) == old_qa
    assert master.read_bytes() == old_master
    assert _dotfiles(output_dir) == []
