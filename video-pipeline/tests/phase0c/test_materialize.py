from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.toolchain.materialize import MaterializeError, materialize_phase0c
from services.toolchain.models import Phase0BToolchainLock, Phase0CToolchainLock, load_lock
from services.toolchain.preview_review import PreviewReviewSection

PARENT = Path("config/toolchains/phase-0b-v1.json")
PIN = Path("config/toolchains/pins/preview-review.json")
OUT = Path("config/toolchains/phase-0c-v1.json")


def test_materialize_merges_parent_and_preview_pin(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0c-v1.json"
    materialize_phase0c(PARENT, PIN, destination)
    lock = load_lock(destination)
    assert isinstance(lock, Phase0CToolchainLock)
    parent = load_lock(PARENT)
    assert isinstance(parent, Phase0BToolchainLock)
    assert lock.ffmpeg.ffmpeg.sha256 == parent.ffmpeg.ffmpeg.sha256
    assert lock.ffmpeg.ffprobe.sha256 == parent.ffmpeg.ffprobe.sha256
    assert lock.python == parent.python
    assert lock.normalization == parent.normalization
    assert lock.preview_review.adapter == "pinned-ffmpeg-preview"
    assert lock.smoke.preview_review.status == "pending"


def test_materialize_is_idempotent_for_identical_output(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0c-v1.json"
    materialize_phase0c(PARENT, PIN, destination)
    payload = destination.read_bytes()
    materialize_phase0c(PARENT, PIN, destination)
    assert destination.read_bytes() == payload


def test_materialize_rejects_differing_existing_output(tmp_path: Path) -> None:
    destination = tmp_path / "phase-0c-v1.json"
    materialize_phase0c(PARENT, PIN, destination)
    destination.write_bytes(destination.read_bytes() + b" ")
    with pytest.raises(MaterializeError, match="differs"):
        materialize_phase0c(PARENT, PIN, destination)


def test_materialize_rejects_missing_pin(tmp_path: Path) -> None:
    with pytest.raises((MaterializeError, Exception), match="pin"):
        materialize_phase0c(PARENT, tmp_path / "missing.json", tmp_path / "out.json")


def test_pin_with_unfrozen_filter_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(PIN.read_bytes())
    argv = payload["smoke"]["generator_argv"]
    argv[argv.index("-vf") + 1] = "transpose=1,fps=30"
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match="frozen"):
        PreviewReviewSection.model_validate_json(pin.read_bytes())


def test_pin_declaring_external_model_is_rejected_by_schema(tmp_path: Path) -> None:
    payload = json.loads(PIN.read_bytes())
    payload["external_model"] = "gpt-reviewer"
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match="external_model"):
        PreviewReviewSection.model_validate_json(pin.read_bytes())
