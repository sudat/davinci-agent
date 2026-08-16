"""Failure modes: missing bindings, sha drift, subtitle mismatch, duration drift, binary drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.contracts.primitives import RationalFrameRate
from services.preview.binding import initial_bindings, initial_timeline_ir
from services.preview.models import (
    ItemBinding,
    MediaBinding,
    PreviewBindingError,
    PreviewToolchainError,
    PreviewVerificationError,
)
from services.preview.render import render_preview
from services.preview.tools import PHASE_0C_LOCK, PinnedTools, load_pinned_tools
from services.preview.verify import verify_preview_output
from tests.preview.conftest import sha256_of

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest

RATE_30_1 = RationalFrameRate(num=30, den=1)


def _rebind(bindings, item_id: str, replacement: ItemBinding):
    return bindings.model_copy(
        update={
            "items": tuple(
                replacement if entry.item_id == item_id else entry for entry in bindings.items
            )
        }
    )


def test_missing_audio_binding_is_a_structured_error(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    bindings = initial_bindings(fixture_dir)
    reduced = bindings.model_copy(
        update={"items": tuple(entry for entry in bindings.items if entry.item_id != "a-cut-002")}
    )
    with pytest.raises(PreviewBindingError, match="a-cut-002"):
        render_preview(None, initial_timeline_ir(manifest), reduced, tmp_path, tools=tools)
    assert not (tmp_path / "preview.mp4").exists()


def test_missing_media_file_is_a_structured_error(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    bindings = initial_bindings(fixture_dir)
    ghost = ItemBinding(
        item_id="cut-001",
        binding=MediaBinding(media_path=str(tmp_path / "nonexistent.mov"), sha256="0" * 64),
    )
    with pytest.raises(PreviewBindingError, match="missing"):
        render_preview(
            None,
            initial_timeline_ir(manifest),
            _rebind(bindings, "cut-001", ghost),
            tmp_path,
            tools=tools,
        )


def test_media_sha_drift_is_refused(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    bindings = initial_bindings(fixture_dir)
    drifted = ItemBinding(
        item_id="cut-001",
        binding=MediaBinding(
            media_path=str(fixture_dir / "source.mov"),
            sha256=sha256_of(fixture_dir / "outro.mov"),
        ),
    )
    with pytest.raises(PreviewBindingError, match="sha256 drift"):
        render_preview(
            None,
            initial_timeline_ir(manifest),
            _rebind(bindings, "cut-001", drifted),
            tmp_path,
            tools=tools,
        )
    assert not (tmp_path / "preview.mp4").exists()


def test_subtitle_table_mismatch_is_refused(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    shifted = tmp_path / "shifted.srt"
    shifted.write_bytes(b"1\n00:00:05,000 --> 00:00:07,000\nPHASE 0A FIXED SUBTITLE\n")
    bindings = initial_bindings(fixture_dir)
    replaced = ItemBinding(
        item_id="s-fixed-001",
        binding=MediaBinding(media_path=str(shifted), sha256=sha256_of(shifted)),
    )
    with pytest.raises(PreviewBindingError, match="subtitle table"):
        render_preview(
            None,
            initial_timeline_ir(manifest),
            _rebind(bindings, "s-fixed-001", replaced),
            tmp_path,
            tools=tools,
        )


def test_duration_drift_is_detected(tools: PinnedTools, fixture_dir: Path) -> None:
    with pytest.raises(PreviewVerificationError, match="drift detected"):
        verify_preview_output(
            tools,
            fixture_dir / "intro.mov",
            total_frames=660,
            rate=RATE_30_1,
            subtitle_expected=True,
        )


def test_binary_hash_drift_refuses_the_run(tmp_path: Path) -> None:
    lock = json.loads(PHASE_0C_LOCK.read_bytes())
    lock["ffmpeg"]["ffmpeg"]["sha256"] = "0" * 64
    tampered = tmp_path / "phase-0c-v1.json"
    tampered.write_bytes(
        json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    with pytest.raises(PreviewToolchainError, match="sha256 drift"):
        load_pinned_tools(tampered)
