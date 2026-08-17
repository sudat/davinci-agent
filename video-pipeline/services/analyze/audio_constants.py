"""Frozen constants for the deterministic dialogue-audio analyzers (Todo 34).

Every threshold, window size, lexicon entry, and confidence value used by the
audio probe, measurements, and candidate generators is declared here exactly
once and hashed into the analyzer cache key, so any change to a frozen value
invalidates cached artifacts instead of silently mixing results.

Measurement scale convention: ``mb`` fields are integer milli-decibels of RMS
or peak amplitude relative to the s16 full scale 32768, computed as
``round_half_away(10000 * log10(mean_square / 2**30))``. Loudness ``mlu``
fields are milli-LUFS from the ebur128 filter (or the honestly-labeled RMS
fallback). No canonical field is ever a float.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

ANALYZER_NAME: Final = "analyze-dialogue"
ANALYZER_VERSION: Final = "todo34-v1"

# time-domain window grid (frozen)
WINDOW_MS: Final = 100
HOP_MS: Final = 50

# silence / pause detection (frozen)
SILENCE_RMS_THRESHOLD_MB: Final = -40000  # -40 dBFS RMS window threshold
MIN_SILENCE_MS: Final = 300  # spans shorter than this are not pauses
SILENCE_FLOOR_MB: Final = -120000  # reported RMS of digital silence

CLIP_SAMPLE_THRESHOLD: Final = 32760  # |sample| >= threshold counts as clipped
FULL_SCALE_SAMPLE: Final = 32768
FULL_SCALE_SQUARE: Final = 1 << 30  # 32768**2

# supported Edit-Source audio declarations (frozen): 16k ASR wav, 48k mezzanine
SUPPORTED_SAMPLE_RATES: Final = (16000, 48000)
DECLARED_AUDIO_CODEC: Final = "pcm_s16le"
DECLARED_CHANNELS: Final = 1

# Japanese filler lexicon (frozen; leftmost occurrence, longest match wins)
FILLER_LEXICON: Final = (
    "えっと",
    "えー",
    "あの",
    "そのー",
    "みたいな",
    "うーん",
    "まあ",
)

# false-start structural rule (frozen): a speech segment that does not end
# with terminal punctuation, followed within MAX_FALSE_START_GAP_MS by a
# speech segment restarting with the same leading character.
TERMINAL_PUNCTUATION: Final = (
    "。",
    "？",  # noqa: RUF001 (Japanese punctuation is the rule data)
    "！",  # noqa: RUF001 (Japanese punctuation is the rule data)
    "?",
    "!",
    "」",
)
MAX_FALSE_START_GAP_MS: Final = 2000

# candidate confidences (frozen ints, 0-1000)
CONFIDENCE_PAUSE_CANDIDATE: Final = 700
CONFIDENCE_FILLER_CANDIDATE: Final = 800
CONFIDENCE_FALSE_START_CANDIDATE: Final = 600

# rule ids (frozen)
PAUSE_RULE_ID: Final = "pause-silence-v1"
FILLER_RULE_ID: Final = "filler-lexicon-ja-v1"
FALSE_START_RULE_ID: Final = "false-start-structural-v1"
DIALOGUE_AMBIENT_RULE_ID: Final = "dialogue-ambient-span-v1"

# loudness (frozen): ebur128 when the pinned ffmpeg provides the filter,
# otherwise the RMS fallback is used and HONESTLY labeled as not BS.1770.
EBUR128_FILTER_NAME: Final = "ebur128"

FROZEN_CONSTANTS_PAYLOAD: Final = {
    "analyzer_version": ANALYZER_VERSION,
    "window_ms": WINDOW_MS,
    "hop_ms": HOP_MS,
    "silence_rms_threshold_mb": SILENCE_RMS_THRESHOLD_MB,
    "min_silence_ms": MIN_SILENCE_MS,
    "silence_floor_mb": SILENCE_FLOOR_MB,
    "clip_sample_threshold": CLIP_SAMPLE_THRESHOLD,
    "full_scale_sample": FULL_SCALE_SAMPLE,
    "supported_sample_rates": SUPPORTED_SAMPLE_RATES,
    "declared_audio_codec": DECLARED_AUDIO_CODEC,
    "declared_channels": DECLARED_CHANNELS,
    "filler_lexicon": FILLER_LEXICON,
    "terminal_punctuation": TERMINAL_PUNCTUATION,
    "max_false_start_gap_ms": MAX_FALSE_START_GAP_MS,
    "confidence_pause": CONFIDENCE_PAUSE_CANDIDATE,
    "confidence_filler": CONFIDENCE_FILLER_CANDIDATE,
    "confidence_false_start": CONFIDENCE_FALSE_START_CANDIDATE,
    "pause_rule_id": PAUSE_RULE_ID,
    "filler_rule_id": FILLER_RULE_ID,
    "false_start_rule_id": FALSE_START_RULE_ID,
    "dialogue_ambient_rule_id": DIALOGUE_AMBIENT_RULE_ID,
}


def frozen_constants_hash() -> str:
    canonical = json.dumps(
        FROZEN_CONSTANTS_PAYLOAD, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
