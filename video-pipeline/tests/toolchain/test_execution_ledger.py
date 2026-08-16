from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.toolchain.execution_ledger import (
    ExecutionLedgerError,
    ToolchainBinding,
    append_toolchain_bound,
)


def test_toolchain_event_chains_to_raw_prior_row(tmp_path: Path) -> None:
    genesis = {
        "event_type": "execution-start",
        "previous_event_hash": "0" * 64,
        "schema_version": "1",
        "sequence": 1,
    }
    raw_genesis = json.dumps(genesis, sort_keys=True, separators=(",", ":")).encode()
    ledger = tmp_path / "execution-ledger.jsonl"
    ledger.write_bytes(raw_genesis + b"\n")
    binding = ToolchainBinding(
        work_id="a" * 64,
        ffmpeg_bin="/attempt/bin/ffmpeg",
        ffmpeg_sha256="b" * 64,
        ffprobe_bin="/attempt/bin/ffprobe",
        ffprobe_sha256="c" * 64,
    )

    event = append_toolchain_bound(ledger, binding)

    assert event.sequence == 2
    assert event.previous_event_hash == hashlib.sha256(raw_genesis).hexdigest()
    assert len(ledger.read_bytes().splitlines()) == 2


def test_toolchain_event_resume_is_idempotent(tmp_path: Path) -> None:
    ledger = tmp_path / "execution-ledger.jsonl"
    ledger.write_text(
        '{"event_type":"execution-start","previous_event_hash":"'
        + "0" * 64
        + '","sequence":1}\n'
    )
    binding = ToolchainBinding(
        work_id="a" * 64,
        ffmpeg_bin="/attempt/bin/ffmpeg",
        ffmpeg_sha256="b" * 64,
        ffprobe_bin="/attempt/bin/ffprobe",
        ffprobe_sha256="c" * 64,
    )

    first = append_toolchain_bound(ledger, binding)
    second = append_toolchain_bound(ledger, binding)

    assert second == first
    assert len(ledger.read_bytes().splitlines()) == 2


def test_toolchain_append_rejects_prior_chain_drift(tmp_path: Path) -> None:
    genesis = (
        '{"event_type":"execution-start","previous_event_hash":"'
        + "0" * 64
        + '","sequence":1}'
    )
    forged = (
        '{"event_type":"prior","previous_event_hash":"'
        + "0" * 64
        + '","sequence":2}'
    )
    ledger = tmp_path / "execution-ledger.jsonl"
    ledger.write_text(genesis + "\n" + forged + "\n")
    binding = ToolchainBinding(
        work_id="a" * 64,
        ffmpeg_bin="/attempt/bin/ffmpeg",
        ffmpeg_sha256="b" * 64,
        ffprobe_bin="/attempt/bin/ffprobe",
        ffprobe_sha256="c" * 64,
    )

    with pytest.raises(ExecutionLedgerError, match="chain drift"):
        append_toolchain_bound(ledger, binding)
