# tests/e2e/fixtures — live (non-seeded) E2E fixture

`live-e2e-ja-speech.mp4` — 16.0 s Japanese-speech camera-format clip
(320x240 h264 30 fps + aac mono) consumed by `tests/e2e/live-episode.spec.ts`
(`LIVE_V44_E2E=1`). Synthesized ONCE on 2026-08-23 and committed; every live
run copies it into a fresh temp source folder, so the committed bytes never
change and the full real chain (ingest → normalize → REAL whisper ASR →
selection → plan → compile → preview) executes against them.

sha256 `4740a8f144cc129d8524a3e0c61e03989088c699e8194aa0651e8ad650e7d4b3`

## Provenance (fully reproducible on macOS)

Written for this repo (original text, no third-party rights involved).
Speech = macOS TTS `say -v Kyoko` — the pinned TTS record in
`video-pipeline/config/toolchains/phase-1-technical-v1.json` (`whisper_ja.tts`:
provider `macos-say`, voice `Kyoko`, ja_JP), same recipe as
`video-pipeline/tests/fixtures/v44/FIXTURES.md` (T12) and
`tests/cli/test_real_episode.py`. Video = `lavfi testsrc2` (the chain's visual
analyzer requires a video track). Encoders are the PINNED phase-1 ffmpeg
(7.1.1 bootstrap: `h264_videotoolbox` + `aac`).

Passage (4 phrases, 1.5 s silence gaps → 15.65 s speech, 16.0 s file):

> こんにちは。今日は撮影素材の編集テストです。
> まず、良い瞬間を選びます。
> 次に、無駄な間を削ります。
> 最後に字幕を付けて完成です。

Exact commands (pinned `$FF` = the lock's ffmpeg path, `$FP` = its ffprobe):

```bash
say -v Kyoko -o tts-0.aiff "こんにちは。今日は撮影素材の編集テストです。"
say -v Kyoko -o tts-1.aiff "まず、良い瞬間を選びます。"
say -v Kyoko -o tts-2.aiff "次に、無駄な間を削ります。"
say -v Kyoko -o tts-3.aiff "最後に字幕を付けて完成です。"
for i in 0 1 2; do
  "$FF" -v error -f lavfi -i anullsrc=r=44100:cl=mono:d=1.5 -c:a pcm_s16le -y "silence-$i.wav"
done
"$FF" -v error -i tts-0.aiff -i silence-0.wav -i tts-1.aiff -i silence-1.wav \
  -i tts-2.aiff -i silence-2.wav -i tts-3.aiff \
  -filter_complex "[0:a][1:a][2:a][3:a][4:a][5:a][6:a]concat=n=7:v=0:a=1" \
  -c:a aac -y speech.m4a
SECS=$(python3 -c "import math;print(math.ceil(float($($FP -v error -show_entries format=duration -of csv=p=0 speech.m4a))))")
"$FF" -v error -f lavfi -i "testsrc2=size=320x240:rate=30:d=$SECS" -i speech.m4a \
  -map 0:v:0 -map 1:a:0 -c:v h264_videotoolbox -pix_fmt yuv420p -c:a aac \
  -y live-e2e-ja-speech.mp4
```

This is Tier C test MEDIA, not ground truth: the chain's real whisper run
decides what the transcript says, and the E2E asserts pipeline mechanics
(stage progression, preview artifact, NL correction → partial rebuild) —
never transcript content.
