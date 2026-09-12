"""Normalize verification levels: standard skips the full decode, full_decode keeps it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import services.normalize.runner as normalize_runner
from services.foundation_io import sha256_file
from services.normalize.models import (
    NormalizeRecord,
    VerificationLevel,
    verify_normalize_record,
    verify_normalize_record_hash,
)
from services.normalize.runner import NormalizeContext, normalize_one
from tests.normalize.conftest import PHASE_0B_LOCK

if TYPE_CHECKING:
    from services.ingest.models import SourceManifest


def _normalize(  # noqa: PLR0913 (thin fixture wrapper over normalize_one)
    manifests: Mapping[str, SourceManifest],
    ffmpeg: Path,
    ffprobe: Path,
    tmp_path: Path,
    *,
    verification: VerificationLevel = "full_decode",
    record_name: str = "record.normalize-record.json",
) -> NormalizeRecord:
    record_out = tmp_path / "records" / record_name
    return normalize_one(
        manifests["p0b-cfr24"],
        "cfr30",
        NormalizeContext(
            lock_path=PHASE_0B_LOCK,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            output_dir=tmp_path / "edit-sources",
            record_out=record_out,
        ),
        verification=verification,
    )


def test_standard_skips_the_full_decode(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("standard verification must not run the full decode")

    monkeypatch.setattr(normalize_runner, "decoded_video_sha256", _forbidden)
    record = _normalize(
        source_manifests,
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
        verification="standard",
        record_name="standard.normalize-record.json",
    )
    assert record.verification == "standard"
    assert record.output_semantics.decoded_video_sha256 is None
    assert record.output_semantics.observed_output_frames == (
        record.drop_dup.expected.output_frames
    )
    output = Path(record.output.path)
    assert record.output.sha256 == sha256_file(output)
    assert record.output.size_bytes == output.stat().st_size
    round_trip = NormalizeRecord.model_validate_json(
        (tmp_path / "records" / "standard.normalize-record.json").read_bytes()
    )
    assert round_trip == record
    assert verify_normalize_record_hash(round_trip)
    assert verify_normalize_record(round_trip)


def test_full_decode_computes_the_hash_by_default(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record = _normalize(
        source_manifests,
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
        record_name="full-decode.normalize-record.json",
    )
    assert record.verification == "full_decode"
    digest = record.output_semantics.decoded_video_sha256
    assert digest is not None
    assert len(digest) == 64
    assert verify_normalize_record(record)


def test_old_records_without_a_level_read_as_full_decode(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record = _normalize(
        source_manifests,
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
        record_name="legacy.normalize-record.json",
    )
    raw = (tmp_path / "records" / "legacy.normalize-record.json").read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    del payload["verification"]
    legacy = NormalizeRecord.model_validate_json(
        json.dumps(payload).encode("utf-8")
    )
    assert legacy.verification == "full_decode"
    assert legacy == record


def test_mismatched_level_and_hash_is_refused(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("standard verification must not run the full decode")

    monkeypatch.setattr(normalize_runner, "decoded_video_sha256", _forbidden)
    standard = _normalize(
        source_manifests,
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
        verification="standard",
        record_name="mismatch-standard.normalize-record.json",
    )
    with pytest.raises(ValidationError, match="verification_mismatch"):
        NormalizeRecord.model_validate_json(
            _with_verification(standard, "full_decode")
        )
    monkeypatch.undo()
    full = _normalize(
        source_manifests,
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
        record_name="mismatch-full.normalize-record.json",
    )
    with pytest.raises(ValidationError, match="verification_mismatch"):
        NormalizeRecord.model_validate_json(_with_verification(full, "standard"))


def _with_verification(record: NormalizeRecord, level: str) -> bytes:
    payload = json.loads(record.model_dump_json())
    assert isinstance(payload, dict)
    payload["verification"] = level
    return json.dumps(payload).encode("utf-8")
