"""Task 12: Japanese subtitle live-proof harness tests (Tier B, offline).

Replay mode only — the recorded fixture artifact replaces the pinned
whisper run, so no test here executes whisper or touches the network.
Fail-closed proofs: missing/corrupt evidence inputs must exit non-zero
with a typed error and leave NO report file behind.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from services.cli.v44_subtitle_proof import merged_proper_nouns
from services.creative_plan.subtitle_text import load_proper_nouns
from services.foundation_io import sha256_file

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "v44"
ARTIFACT = FIXTURES / "transcript-artifact.json"
SAMPLE = FIXTURES / "sample-corrected.json"
PROPER_NOUNS = FIXTURES / "proper-nouns-episode.json"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.cli.v44_subtitle_proof", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
        timeout=120,
    )


def test_replay_produces_report_with_both_blocks(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(
        [
            "--asr-artifact",
            str(ARTIFACT),
            "--sample",
            str(SAMPLE),
            "--proper-nouns",
            str(PROPER_NOUNS),
            "--out-dir",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr

    report_path = out / "subtitle-proof.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Evidence side: real numbers from T5's metrics vs the committed sample.
    evidence = report["evidence"]
    assert evidence["metrics_source"] == "services.metrics.v44_product_proof"
    assert isinstance(evidence["transcript_cer"], float)
    assert 0.0 < evidence["transcript_cer"] <= 1.0
    assert evidence["proper_noun_recall"] == 1.0  # episode dictionary merged in
    assert isinstance(evidence["timestamp_error_p95_ms"], (int, float))
    assert evidence["timestamp_error_p95_ms"] >= 0
    assert isinstance(evidence["omitted_utterances"], int)
    assert isinstance(evidence["duplicated_utterances"], int)

    # Subtitle side: cue checks ran on the produced cues.
    subtitle = report["subtitle"]
    assert subtitle["cue_count"] >= 1
    assert subtitle["snippet_render"] == "external_srt"
    assert (out / "preview-snippet.srt").is_file()
    assert subtitle["snippet_sha256"] == sha256_file(out / "preview-snippet.srt")
    assert subtitle["matrix_status"] in {"accepted", "partial", "failed", "not_available"}
    assert subtitle["legibility_note"]
    # The atomic DaVinci Resolve span (15 cols) exceeds the 13-col profile.
    assert "subtitle_max_chars" in subtitle["qc_rule_ids"]
    assert subtitle["qc_issue_count"] == len(subtitle["qc_rule_ids"]) or subtitle[
        "qc_issue_count"
    ] >= 1

    # PRD 7.4: evidence and subtitle verdicts are SEPARATE fields.
    assert report["evidence_verdict"] == "measured"
    assert report["subtitle_verdict"] == "issues-found"

    # Replay hash-note: the recorded artifact bytes are bound into the report.
    assert report["asr"]["mode"] == "replay"
    assert report["asr"]["artifact_sha256"] == sha256_file(ARTIFACT)
    assert any("replay" in note for note in report["notes"])


def test_fail_closed_missing_sample_writes_no_report(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(
        ["--asr-artifact", str(ARTIFACT), "--sample", str(tmp_path / "nope.json"),
         "--out-dir", str(out)]
    )
    assert result.returncode != 0
    assert "sample-missing" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_fail_closed_corrupt_sample_writes_no_report(tmp_path: Path) -> None:
    bad = tmp_path / "bad-sample.json"
    bad.write_text("{not json", encoding="utf-8")
    out = tmp_path / "out"
    result = _run(
        ["--asr-artifact", str(ARTIFACT), "--sample", str(bad), "--out-dir", str(out)]
    )
    assert result.returncode != 0
    assert "sample-parse-error" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_fail_closed_sample_without_proper_nouns(tmp_path: Path) -> None:
    sample = tmp_path / "no-nouns.json"
    sample.write_text(
        json.dumps(
            {
                "schema_version": "v44-transcript-sample-v1",
                "segments": [{"start_ms": 0, "end_ms": 900, "text": "テストです"}],
                "proper_nouns": {},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    result = _run(
        ["--asr-artifact", str(ARTIFACT), "--sample", str(sample), "--out-dir", str(out)]
    )
    assert result.returncode != 0
    assert "sample-parse-error" in result.stderr
    assert "proper nouns" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_fail_closed_no_asr_input(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(["--sample", str(SAMPLE), "--out-dir", str(out)])
    assert result.returncode != 0
    assert "asr-input-missing" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_fail_closed_ambiguous_asr_inputs(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(
        [
            "--asr-artifact",
            str(ARTIFACT),
            "--audio",
            str(FIXTURES / "speech-ja-kyoko-16k.wav"),
            "--sample",
            str(SAMPLE),
            "--out-dir",
            str(out),
        ]
    )
    assert result.returncode != 0
    assert "asr-input-ambiguous" in result.stderr


def test_fail_closed_missing_replay_artifact(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(
        [
            "--asr-artifact",
            str(tmp_path / "ghost.json"),
            "--sample",
            str(SAMPLE),
            "--out-dir",
            str(out),
        ]
    )
    assert result.returncode != 0
    assert "asr-artifact-missing" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_fail_closed_corrupt_replay_artifact(tmp_path: Path) -> None:
    bad = tmp_path / "bad-artifact.json"
    bad.write_text('{"segments": "not a tuple"}', encoding="utf-8")
    out = tmp_path / "out"
    result = _run(
        ["--asr-artifact", str(bad), "--sample", str(SAMPLE), "--out-dir", str(out)]
    )
    assert result.returncode != 0
    assert "asr-artifact-parse-error" in result.stderr
    assert not (out / "subtitle-proof.json").exists()


def test_merged_proper_nouns_unions_colliding_canonicals(tmp_path: Path) -> None:
    channel = load_proper_nouns()
    merged = merged_proper_nouns(PROPER_NOUNS)
    by_canonical = {entry.canonical: entry for entry in merged.entries}
    # Episode-only canonical appended.
    assert "OpenCode" in by_canonical
    # Colliding canonical: channel variants kept, episode variant appended.
    davinci = by_canonical["DaVinci Resolve"]
    channel_davinci = next(
        entry for entry in channel.entries if entry.canonical == "DaVinci Resolve"
    )
    assert set(channel_davinci.variants) < set(davinci.variants)
    assert "ダビンシーリゾルブ" in davinci.variants
    # Everything from the channel dictionary survives the merge (canonicals
    # kept; channel variants never dropped on collision).
    merged_by_canonical = {entry.canonical: entry for entry in merged.entries}
    for entry in channel.entries:
        assert entry.canonical in merged_by_canonical
        assert set(entry.variants) <= set(merged_by_canonical[entry.canonical].variants)
