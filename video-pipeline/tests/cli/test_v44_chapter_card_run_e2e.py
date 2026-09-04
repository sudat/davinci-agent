"""End-to-end run_insertion/CLI/evidence/protection on a scaled synthetic episode.

The frozen 7792-frame contract is rebound to a 60/20/45-frame synthetic world so
the real orchestration (preflight gates -> decode -> splice render -> probes ->
visual QA -> audio proof -> evidence -> protection) runs in seconds without a
full real episode render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli._v44_chapter_card_run as run_module
from services.cli._v44_chapter_card_gates import sha256_bytes
from services.cli._v44_chapter_card_media import decode_pcm_s16le
from services.cli._v44_chapter_card_run import EVIDENCE_NAME, MASTER_NAME
from services.cli.v44_chapter_card_insert import main
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


@pytest.fixture(scope="module")
def scaled_world(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PinnedTools, Path, Path, Path]:
    """(tools, episode_root, diag_root, source) for the scaled contract."""

    tools = pinned_tools_or_skip()
    if not APPROVED_FONT.is_file():
        pytest.skip("approved font not installed")
    root = tmp_path_factory.mktemp("chapter-card-e2e")
    episode_root, diag_root = episode_workspace(root)
    source = root / "source.mp4"
    synthetic_source(tools.ffmpeg, source, width=CANVAS[0], height=CANVAS[1])
    return tools, episode_root, diag_root, source


def _install(monkeypatch: pytest.MonkeyPatch, world: tuple[PinnedTools, Path, Path, Path]) -> bytes:
    tools, episode_root, _diag_root, source = world
    source_pcm = decode_pcm_s16le(tools, source)
    assert len(source_pcm) == 96000 * 4
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
    return source_pcm


def test_run_insertion_produces_master_and_measured_evidence(
    tmp_path: Path,
    scaled_world: tuple[PinnedTools, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the scaled synthetic episode and the real orchestration
    # When: run_insertion executes
    # Then: master exists, evidence carries measured facts, protection holds
    tools, episode_root, diag_root, source = scaled_world
    sidecar = write_sidecar(source.parent, scaled=True)
    _install(monkeypatch, scaled_world)
    output_dir = tmp_path / "out"
    before = run_module.hash_protected(
        run_module.protected_paths(
            episode_root=episode_root, diag_finishing_root=diag_root
        ),
        root=run_module.common_root(episode_root, diag_root),
    )
    master = run_module.run_insertion(
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
    assert master == output_dir / MASTER_NAME
    evidence = json.loads((output_dir / EVIDENCE_NAME).read_bytes())
    output = evidence["output"]
    assert output["video_frames"] == 105
    assert output["audio_samples"] == 168000
    assert output["video_codec"] == "h264"  # measured, not hardcoded
    assert output["video_time_base"] == "1/15360"
    assert output["sha256"] == sha256_file(master)
    assert evidence["render"]["video_timescale"] == 15360  # measured from stream
    assert evidence["audio_proof"]["prefix_bytes"] == 128000
    assert evidence["audio_proof"]["silence_bytes"] == 288000
    assert evidence["audio_proof"]["silence_all_zero"] is True
    assert evidence["visual_qa"]["card_span"]["card_frames"] == 45
    assert evidence["visual_qa"]["card_span"]["per_frame_white_min"] >= 200
    assert evidence["visual_qa"]["card_span"]["background_max"] <= 24
    assert evidence["visual_qa"]["card_span"]["max_neighbor_diff"] <= 96
    assert evidence["tools"]["font_face"] == ["Hiragino Sans GB", "W6"]
    assert evidence["tools"]["font_sha256"] == sha256_file(APPROVED_FONT)
    assert evidence["protection"]["unchanged"] is True
    after = run_module.hash_protected(
        run_module.protected_paths(
            episode_root=episode_root, diag_finishing_root=diag_root
        ),
        root=run_module.common_root(episode_root, diag_root),
    )
    assert after == before


def test_cli_runs_the_scaled_insertion_end_to_end(
    tmp_path: Path,
    scaled_world: tuple[PinnedTools, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Given: the same scaled world exposed through the CLI
    # When: main() runs with argument paths
    # Then: exit 0, printed master sha, evidence written, protected untouched
    tools, episode_root, diag_root, source = scaled_world
    sidecar = write_sidecar(source.parent, scaled=True)
    _install(monkeypatch, scaled_world)
    monkeypatch.setattr("services.cli.v44_chapter_card_insert.load_tools", lambda: tools)
    output_dir = tmp_path / "cli-out"
    protected = run_module.protected_paths(
        episode_root=episode_root, diag_finishing_root=diag_root
    )
    snapshot = {path: path.read_bytes() for path in protected}
    code = main(
        [
            "--source", str(source),
            "--proposal", str(sidecar),
            "--font", str(APPROVED_FONT),
            "--output-dir", str(output_dir),
            "--episode-root", str(episode_root),
            "--diag-finishing-root", str(diag_root),
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert f"{output_dir / MASTER_NAME} sha256=" in stdout
    assert (output_dir / EVIDENCE_NAME).is_file()
    assert all(path.read_bytes() == snapshot[path] for path in protected)


def test_cli_refuses_bad_source_without_traceback(
    tmp_path: Path,
    scaled_world: tuple[PinnedTools, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Given: a source file that does not hash to the approved render
    # When: main() runs
    # Then: exit 2, typed code on stderr, no evidence written
    tools, episode_root, diag_root, source = scaled_world
    sidecar = write_sidecar(source.parent, scaled=True)
    _install(monkeypatch, scaled_world)
    monkeypatch.setattr("services.cli.v44_chapter_card_insert.load_tools", lambda: tools)
    bad_source = tmp_path / "bad.mp4"
    bad_source.write_bytes(b"definitely not the approved render")
    output_dir = tmp_path / "refused"
    code = main(
        [
            "--source", str(bad_source),
            "--proposal", str(sidecar),
            "--font", str(APPROVED_FONT),
            "--output-dir", str(output_dir),
            "--episode-root", str(episode_root),
            "--diag-finishing-root", str(diag_root),
        ]
    )
    assert code == 2
    stderr = capsys.readouterr().err
    assert "source-hash-mismatch" in stderr
    assert "Traceback" not in stderr
    assert not (output_dir / EVIDENCE_NAME).is_file()
