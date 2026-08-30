# Project Memory

## V44-2 live finishing replay (2026-08-25)

- Symptom: Resolve had all 97 video and 97 audio items, but live finishing reported placement readback mismatches and reruns risked duplicate placement.
- Root cause: Resolve 21.0.4.5 may report source start/end one frame lower than the half-open plan. Record/timeline positions remained exact. The resume probe also discarded `track_type`, so a matching video occurrence could be mistaken for existing audio on a fresh build.
- Fix: tolerate exactly +/-1 frame for source readback only; keep record spans strict; retain and require `track_type` when deciding whether a video or audio placement already exists.
- Regression guard: test an AV-linked pair with identical source and record spans and require the audio append despite an existing video occurrence.
- Product verification: final-v4 contains 118 round-tripped subtitles; stereo loudness is -14.26 LUFS with -1.40 dB true peak; deterministic QC passed with zero issues. The finishing CLI still blocks honestly on five unsupported subtitle/audio/color MCP actions, so do not fabricate `finishing-run.json`.

## V44-2 subtitle timing repair (2026-08-25)

- Symptom: `final-v4` subtitles became progressively early: about 7.1 seconds on average and 16.7 seconds near the end; the subtitle plan ended at frame 7275 while the committed timeline ended at frame 7792.
- Root cause: finishing converted already-edited record-timeline cue frames into ASR seconds and then ran source-to-record reconciliation again, applying edit removals twice.
- Fix: map each record-timed cue back through its containing primary clip to the original source frames before rebuilding the subtitle plan. Cues may occupy only part of a clip, so use the cue's offset within the containing clip rather than exact span equality.
- Regression guard: `tests/cli/test_v44_finishing_ir.py` places source frames 100-160 at record frames 0-60 and requires a cue at record frames 15-45 to become source frames 115-145.
- Product verification: `final-v5` has 100 embedded subtitles ending at frame 7792; embedded subtitles exactly round-trip to the corrected SRT; video/audio stream hashes match `final-v4`; technical QC passed with zero issues. Fresh ASR comparison on 50 unique long utterances improved median subtitle-center error from 11.535 seconds (`v3`) to 0.220 seconds (`v5`), with 42/50 within one second.

## MCP finishing execution ordering and Task 5 blocker (2026-08-28)

- Symptom: a live rerun spent one hour after only the first audio/video placement pair, despite bounded placement scans.
- Root cause: execution steps were sorted by record position before phase, so the 100-cue subtitle verifier ran after the first pair. Audio steps at the same phase also sorted by ID, placing final loudness QC before voice isolation.
- Fix: one shared `step_sort_key` now orders phase first and preserves the committed audio ladder. Real plan order is all 194 placements → subtitles → cleanup → normalization → voice isolation → final loudness QC.
- Remaining external blocker: focused real-episode cleanup measured `0.0 dB` noise improvement versus required `3–12 dB`. The operator must tune and resave Fairlight preset `dialogue-chain`; code must not clamp or fabricate success.
