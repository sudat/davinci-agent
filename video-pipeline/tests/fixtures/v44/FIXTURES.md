# tests/fixtures/v44 — Tier B subtitle-proof fixtures

Synthesized-speech fixture for OFFLINE Tier B reproduction of the T12
Japanese subtitle proof harness (`services/cli/v44_subtitle_proof.py`).
Real-footage proof happens at the gates (V44-0/V44-1/V44-2); this fixture
never substitutes for it.

## Files

| File | Role |
|---|---|
| `speech-ja-kyoko-16k.wav` | 23.68 s Japanese speech, 16 kHz mono pcm_s16le (the pinned ASR input format) |
| `transcript-artifact.json` | The recorded REAL whisper transcript artifact (replay input for `--asr-artifact`) |
| `sample-corrected.json` | Hand-authored corrected sample (ground truth), T5 `TranscriptSampleV1` schema `v44-transcript-sample-v1` |
| `proper-nouns-episode.json` | Episode-local proper-noun additions (`proper-nouns-ja-v1`), merged over the channel dictionary by the harness |

## Provenance (all reproducible on this machine)

1. **Speech synthesis** — macOS TTS `say -v Kyoko` (the pinned TTS record in
   `config/toolchains/phase-1-technical-v1.json` `whisper_ja.tts`:
   provider `macos-say`, voice `Kyoko`, ja_JP) reading this exact passage
   (23.68 s, measured with `afinfo`):

   > 今日は動画編集のワークフローについて話します。DaVinci Resolveは色補正とタイムライン編集が一体になった強力なツールです。一方、OpenCodeはターミナルで動くコーディングエージェントです。この二つを組み合わせると、撮影から公開までの作業を大きく減らせます。それでは、始めましょう。

2. **Preprocessing** — the synthesized AIFF was converted to 16 kHz mono
   pcm_s16le WAV by the harness itself via `transcribe()`'s frozen pinned
   ffmpeg argv (`asr_whisper_cpp.FROZEN_PREPROCESSING_ARGV`). The committed
   WAV is the exact `asr-16k.wav` the pinned whisper consumed.

3. **ASR** — the pinned whisper.cpp CLI (`whisper-cli` ggml 0.20.0,
   commit `1fe009ca`, model `ggml-large-v3-turbo.bin` sha256
   `1fc70f77…bc69`, frozen argv, temperature 0.000, threads 4) ran ONCE on
   this machine (2026-08-23) to record `transcript-artifact.json`. The
   artifact is committed AS RECORDED — including its `input_binding`
   pointing at the (temp, since removed) source AIFF path — because it is
   recorded evidence, not a hand-edited file. Replay validates it through
   `TranscriptArtifact` (content-hash bound), so any edit would invalidate
   it.

   What whisper actually heard (real mishearings, kept as evidence):
   `ダビンシーリゾルブ` (for "DaVinci Resolve") and `オパンコード`
   (for "OpenCode").

## Ground-truth legitimacy (Tier B)

The passage text fed to `say` is KNOWN to the fixture author, so
`sample-corrected.json` texts are the exact synthesized ground truth —
legitimate Tier B ground truth, not a transcription of the audio.
Timestamps are hand-authored PROPORTIONAL estimates (sentence boundaries
by character count scaled to the measured 23.679 s duration, accuracy
roughly ±1 s — the recorded p95 timestamp error of 740 ms is consistent
with that estimate noise, not ASR drift alone). Operator samples for real
episodes must be authored by listening (`private/reference-episodes/
v44-real-01/transcript-sample-corrected.json`).

`proper-nouns-episode.json` variants include the misheard forms above —
discovered from the recorded whisper run on this machine (exactly what an
operator adds for their episode's difficult nouns). The colliding
`DaVinci Resolve` canonical with the channel dictionary exercises the
harness's variant-union merge path.

## Semantics worth knowing when reading reports from this fixture

- `omitted_utterances` / `duplicated_utterances` come from T5's canonical
  definitions (`services/metrics/v44_product_proof.py`): EXACT text
  equality after strip. Whisper emits unpunctuated, space-separated text,
  so every sample sentence counts as both "omitted" and "duplicated" on
  this fixture. This is T5's strict definition, reported as-is — CER
  (0.216 on the recorded artifact) carries the usable text-quality signal.
- `proper_noun_recall` is dictionary-aware (the harness builds the
  hypothesis surface via the channel+episode dictionary, the pipeline's
  deterministic post-ASR correction): 0.0 with the channel dictionary
  alone, 1.0 once the episode-local variants are merged — demonstrating
  the merge mechanics end to end.
- The subtitle side reports one honest layout finding on this fixture:
  the atomic proper-noun span `DaVinci Resolve` (15 columns) exceeds the
  13-chars-per-line profile (atomic spans are never split), surfacing as
  `subtitle_max_chars`.

## Replay (no whisper, no network)

```bash
uv run python -m services.cli.v44_subtitle_proof \
  --asr-artifact tests/fixtures/v44/transcript-artifact.json \
  --sample tests/fixtures/v44/sample-corrected.json \
  --proper-nouns tests/fixtures/v44/proper-nouns-episode.json \
  --out-dir /tmp/v44-subtitle-proof-out
```

Live mode (runs the pinned whisper for real) replays the same path via
`--audio tests/fixtures/v44/speech-ja-kyoko-16k.wav` — note the recorded
artifact's `input_binding` names the original AIFF, so a live re-run on
the WAV records a NEW artifact (different binding/hash), which is correct
content-addressed behavior.
