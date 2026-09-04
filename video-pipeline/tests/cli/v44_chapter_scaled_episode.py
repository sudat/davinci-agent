"""A scaled synthetic episode for run-level chapter-card insertion tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli._v44_chapter_card_run as run_module
from services.cli._v44_chapter_card_gates import sha256_bytes
from services.cli._v44_chapter_card_media import decode_pcm_s16le
from services.cli._v44_chapter_card_run import EVIDENCE_NAME, MASTER_NAME
from services.foundation_io import sha256_file
from tests.cli.v44_chapter_episode_workspace import (
    episode_workspace,
    pinned_tools_or_skip,
)
from tests.cli.v44_chapter_scaled_contract import (
    ContractIdentity,
    install_scaled_contract,
    synthetic_source,
)
from tests.cli.v44_chapter_sidecar_factory import write_sidecar

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

CANVAS = (960, 540)
APPROVED_FONT = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")
type World = tuple[PinnedTools, Path, Path, Path]


def make_scaled_world(root: Path) -> World:
    """(tools, episode_root, diag_root, source) with a 60/20/45-frame source."""

    tools = pinned_tools_or_skip()
    if not APPROVED_FONT.is_file():
        pytest.skip("approved font not installed")
    episode_root, diag_root = episode_workspace(root)
    source = root / "source.mp4"
    synthetic_source(tools.ffmpeg, source, width=CANVAS[0], height=CANVAS[1])
    return tools, episode_root, diag_root, source


def install_world(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    tools, episode_root, _, source = world
    source_pcm = decode_pcm_s16le(tools, source)
    fake_plan = episode_root / "review" / "store" / "plan-v3.json"
    items = [{"kind": "video", "item_id": f"s{i}"} for i in range(4)]
    items += [{"kind": "subtitle", "item_id": f"st{i}"} for i in range(3)]
    fake_plan.write_text(json.dumps({"plan": {"items": items}}), encoding="utf-8")
    install_scaled_contract(
        monkeypatch,
        canvas=CANVAS,
        identity=ContractIdentity(
            source_video_sha256=sha256_file(source),
            source_pcm_sha256=sha256_bytes(source_pcm),
            plan_sha256=sha256_file(fake_plan),
        ),
        subtitle_items=3,
    )


def run_world(tools: PinnedTools, world: World, output_dir: Path) -> Path:
    """One full insertion run against the scaled world."""

    _, episode_root, diag_root, source = world
    before = run_module.hash_protected(
        run_module.protected_paths(episode_root=episode_root, diag_finishing_root=diag_root),
        root=run_module.common_root(episode_root, diag_root),
    )
    sidecar = write_sidecar(source.parent, scaled=True)
    return run_module.run_insertion(
        tools,
        run_module.InsertionRequest(
            source=source,
            proposal=sidecar,
            font=APPROVED_FONT,
            output_dir=output_dir,
            episode_root=episode_root,
            diag_finishing_root=diag_root,
            protected_before=before,
        ),
    )


def run_world_once(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Install the contract and publish once; returns (master, evidence, output_dir)."""

    tools, _, _, _ = world
    install_world(monkeypatch, world)
    output_dir = tmp_path / "out"
    run_world(tools, world, output_dir)
    return output_dir / MASTER_NAME, output_dir / EVIDENCE_NAME, output_dir


def snapshot(path: Path) -> dict[str, bytes]:
    """Every file under `path` (recursive) keyed by relative posix name."""

    if path.is_file():
        return {path.name: path.read_bytes()}
    return {
        entry.relative_to(path).as_posix(): entry.read_bytes()
        for entry in sorted(path.rglob("*"))
        if entry.is_file()
    }


__all__ = [
    "APPROVED_FONT",
    "CANVAS",
    "World",
    "install_world",
    "make_scaled_world",
    "run_world",
    "run_world_once",
    "snapshot",
]
