"""Failed reruns never masquerade as success; filesystem failures are typed."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli._v44_chapter_card_run as run_module
from services.cli import _v44_chapter_card_files as files
from services.cli._v44_chapter_card_gates import ChapterCardInsertError
from services.cli._v44_chapter_card_run import EVIDENCE_NAME, MASTER_NAME
from services.foundation_io import sha256_file
from tests.cli.v44_chapter_scaled_episode import (
    World,
    install_world,
    make_scaled_world,
    run_world,
    run_world_once,
)

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools


def _install(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    install_world(monkeypatch, world)


def _run(tools: PinnedTools, world: World, output_dir: Path) -> Path:
    return run_world(tools, world, output_dir)


@pytest.fixture(scope="module")
def scaled_world(tmp_path_factory: pytest.TempPathFactory) -> World:
    return make_scaled_world(tmp_path_factory.mktemp("chapter-card-publish"))


def test_failed_rerun_leaves_old_master_and_evidence_untouched(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a previously successful run's master and evidence on disk
    master, evidence, _output = run_world_once(tmp_path, scaled_world, monkeypatch)
    output_dir = master.parent
    old_master, old_evidence = master.read_bytes(), evidence.read_bytes()
    tools, _, _, _ = scaled_world

    # When: a rerun fails at the final protection gate, before publishing
    def _protected_drift(before: dict[str, str], after: dict[str, str]) -> None:
        raise ChapterCardInsertError("protected-drift", "injected")

    monkeypatch.setattr(run_module, "require_protection_unchanged", _protected_drift)
    with pytest.raises(ChapterCardInsertError, match="protected-drift"):
        _run(tools, scaled_world, output_dir)

    # Then: both prior artifacts stay byte-identical and no temporaries linger
    assert master.read_bytes() == old_master
    assert evidence.read_bytes() == old_evidence
    assert {p.name for p in output_dir.iterdir()} == {EVIDENCE_NAME, MASTER_NAME, "qa", "work"}
    assert list(output_dir.glob(".*.mov")) == []
    assert list(output_dir.glob(".*.json")) == []


def test_failed_rerun_after_render_cleans_only_this_runs_temporaries(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an old published master, and a rerun that renders then fails visual QA
    master, _evidence, _output = run_world_once(tmp_path, scaled_world, monkeypatch)
    output_dir = master.parent
    old_bytes = master.read_bytes()
    tools, _, _, _ = scaled_world

    def _span_refusal(*args: object, **kwargs: object) -> dict[str, object]:
        raise ChapterCardInsertError("card-frame-dim", "injected")

    monkeypatch.setattr(run_module, "verify_card_span", _span_refusal)
    with pytest.raises(ChapterCardInsertError, match="card-frame-dim"):
        _run(tools, scaled_world, output_dir)

    # Then: the old master is untouched and the temporary render is gone
    assert master.read_bytes() == old_bytes
    assert list(output_dir.rglob("*.mov")) == [master]


def test_successful_rerun_republishes_both_artifacts_consistently(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a successful run followed by a second successful rerun
    tools, _, _, _ = scaled_world
    master, evidence, _output = run_world_once(tmp_path, scaled_world, monkeypatch)
    output_dir = master.parent
    first_master = master.read_bytes()

    # When: the rerun publishes
    second = _run(tools, scaled_world, output_dir)

    # Then: master and evidence are republished, mutually consistent, no temps
    assert second == master
    assert master.read_bytes() != first_master, "hardware encode must produce fresh bytes"
    document = json.loads(evidence.read_bytes())
    assert document["output"]["sha256"] == sha256_file(master)
    assert document["output"]["path"] == str(master)
    assert list(output_dir.glob(".*")) == []


def test_publication_arrives_via_exclusive_names_replaced_into_place(
    tmp_path: Path, scaled_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a successful run with the rename seam watched
    tools, _, _, _ = scaled_world
    _install(monkeypatch, scaled_world)
    output_dir = tmp_path / "out"
    renames: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def _tracked_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _tracked_replace)

    # When: the run publishes
    _run(tools, scaled_world, output_dir)

    # Then: every rename source lives inside this run's exclusive staging
    # directory, and the finals land master -> evidence -> qa, with no leftovers
    for src, _ in renames:
        assert src.parent.parent == output_dir, src
        assert src.parent.name.startswith(".v44-"), src
    assert all(dst.parent == output_dir for _, dst in renames)
    placements = [dst.name for _, dst in renames if not dst.name.startswith(".")]
    assert placements == [MASTER_NAME, EVIDENCE_NAME, "qa"]
    assert not list(output_dir.glob(".*"))


def test_output_dir_creation_failure_is_typed(tmp_path: Path) -> None:
    # Given: an output-dir path occupied by a regular file
    # When: the run creates its directories
    # Then: typed insertion refusal, never a raw OSError
    blocker = tmp_path / "output-dir"
    blocker.write_bytes(b"occupied")
    with pytest.raises(ChapterCardInsertError, match="fs-dir-failed"):
        files.require_dir(blocker / "work")


def test_readonly_dir_write_and_temp_failures_are_typed(tmp_path: Path) -> None:
    # Given: a directory without write permission
    # When: the run writes intermediates or reserves temporaries
    # Then: typed insertion refusals naming the path
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        with pytest.raises(ChapterCardInsertError, match="fs-write-failed"):
            files.write_file(readonly / "master.pcm", b"\x00" * 16)
        with pytest.raises(ChapterCardInsertError, match="fs-temp-failed"):
            files.exclusive_temp(readonly, ".mov")
    finally:
        with suppress(OSError):
            readonly.chmod(0o700)


def test_missing_file_stat_is_typed(tmp_path: Path) -> None:
    # Given: a rendered master path that does not exist
    # When: the run measures its size for the evidence
    # Then: typed insertion refusal
    with pytest.raises(ChapterCardInsertError, match="fs-stat-failed"):
        files.file_size(tmp_path / "absent.mov")
