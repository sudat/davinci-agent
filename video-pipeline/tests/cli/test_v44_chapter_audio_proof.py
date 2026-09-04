"""Audio proof: zero-copy splice verification with unchanged evidence hashes (TDD)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import services.cli._v44_chapter_card_gates as gates
import services.cli._v44_chapter_card_plan as plan
from services.cli._v44_chapter_card_gates import ChapterCardInsertError, audio_proof
from tests.cli.v44_chapter_episode_workspace import fake_tools

PREFIX, SILENCE = 1600, 800
SOURCE_BYTES = 4000


@pytest.fixture
def small_pcm(monkeypatch: pytest.MonkeyPatch) -> bytes:
    # the proof reads the splice byte boundaries from its own module bindings
    for target in (plan, gates):
        monkeypatch.setattr(target, "PREFIX_BYTES", PREFIX)
        monkeypatch.setattr(target, "SILENCE_BYTES", SILENCE)
        monkeypatch.setattr(target, "OUTPUT_PCM_BYTES", SOURCE_BYTES + SILENCE)
    monkeypatch.setattr(plan, "SOURCE_PCM_BYTES", SOURCE_BYTES)
    return (bytes(range(256)) * 16)[:SOURCE_BYTES]


def _decode_returns(monkeypatch: pytest.MonkeyPatch, decoded: bytes) -> None:
    monkeypatch.setattr(gates, "decode_pcm_s16le", lambda tools, media: decoded)


def _tools(tmp_path: Path):
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")
    return fake_tools(marker, marker)


def test_audio_proof_reports_exact_part_hashes(
    tmp_path: Path, small_pcm: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a master PCM that is exactly prefix + zeros + source suffix
    # When: the audio proof runs
    # Then: every recorded hash matches an independent hashlib computation
    source_pcm = small_pcm
    constructed = plan.build_output_pcm(source_pcm)
    _decode_returns(monkeypatch, constructed)
    proof = audio_proof(
        _tools(tmp_path), tmp_path / "master.mov",
        source_pcm, constructed,
    )
    prefix, silence, suffix = source_pcm[:PREFIX], b"\x00" * SILENCE, source_pcm[PREFIX:]
    reconstructed = hashlib.sha256(prefix)
    reconstructed.update(suffix)
    assert proof["prefix_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert proof["silence_sha256"] == hashlib.sha256(silence).hexdigest()
    assert proof["suffix_sha256"] == hashlib.sha256(suffix).hexdigest()
    assert proof["reconstructed_sha256"] == reconstructed.hexdigest()
    assert proof["silence_all_zero"] is True
    assert proof["build_output_pcm_used"] is True
    assert proof["prefix_bytes"] == PREFIX
    assert proof["silence_bytes"] == SILENCE
    assert proof["suffix_bytes"] == SOURCE_BYTES - PREFIX


@pytest.mark.parametrize(
    ("tamper", "where"),
    [
        ("prefix", "decoded"),
        ("silence", "decoded"),
        ("suffix", "decoded"),
        ("length", "decoded"),
        ("constructed_prefix", "constructed"),
    ],
)
def test_audio_proof_refuses_every_splice_tamper(
    tmp_path: Path, small_pcm: bytes, monkeypatch: pytest.MonkeyPatch, tamper: str, where: str
) -> None:
    # Given: a decoded master (or constructed expectation) broken in one exact way
    # When: the audio proof runs
    # Then: typed identity refusal for every tamper class
    source_pcm = small_pcm
    constructed = plan.build_output_pcm(source_pcm)
    payload = bytearray(constructed)
    if tamper == "prefix":
        payload[0] ^= 1
    elif tamper == "silence":
        payload[PREFIX] = 1
    elif tamper == "suffix":
        payload[PREFIX + SILENCE] ^= 1
    decoded = bytes(payload[:-1]) if tamper == "length" else bytes(payload)
    if where == "decoded":
        _decode_returns(monkeypatch, decoded)
        proof_source, proof_constructed = source_pcm, constructed
    else:
        _decode_returns(monkeypatch, constructed)
        bad_constructed = bytearray(constructed)
        bad_constructed[0] ^= 1
        proof_source, proof_constructed = source_pcm, bytes(bad_constructed)
    with pytest.raises(ChapterCardInsertError, match="audio-identity-failed"):
        audio_proof(
            _tools(tmp_path), tmp_path / "master.mov",
            proof_source, proof_constructed,
        )


def test_audio_proof_does_not_rebuild_output_pcm(
    tmp_path: Path, small_pcm: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a valid splice
    # When: the audio proof runs
    # Then: neither build_output_pcm nor split_output_pcm is invoked again
    #       (no 50 MB copy is materialized inside the proof)
    def _no_rebuild(source: bytes) -> bytes:
        raise AssertionError("audio_proof must not rebuild the output PCM")

    def _no_split(payload: bytes) -> tuple[bytes, bytes, bytes]:
        raise AssertionError("audio_proof must not copy the split parts")

    monkeypatch.setattr(plan, "build_output_pcm", _no_rebuild)
    monkeypatch.setattr(plan, "split_output_pcm", _no_split)
    constructed = bytes(small_pcm[:PREFIX]) + b"\x00" * SILENCE + bytes(small_pcm[PREFIX:])
    _decode_returns(monkeypatch, constructed)
    proof = audio_proof(
        _tools(tmp_path), tmp_path / "master.mov",
        small_pcm, constructed,
    )
    assert proof["build_output_pcm_used"] is True
