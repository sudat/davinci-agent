# allow: SIZE_OK — the clip-scoped JA request prompt (proven A/B wording,
# inseparable from the event contract) plus the strict chunk contracts and
# validation dominate; splitting them would separate the contract text from
# the validation that enforces it.
"""Gemini AV observation contracts: models, chunk math, strict validation.

Pure half of the chunked observation (reviewer order P1-1): cores cover
``[0, duration)`` exactly once; each call sees core + 3s context, reports
clip-local seconds, and only core starts are adopted (no boundary
duplicates, no merge). ANY out-of-range event marks its chunk
insufficient — never clamped, never a candidate. No wire/cost/sidecars
(``gemini_av_wire`` / ``gemini_av_record``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from services.contracts.primitives import StrictModel
from services.media_intelligence.sample_observation import (
    SampleObservationError,
    Second,
)

#: Live-verified primary AV model (2026-09-11 A/B: 2.5-flash-lite is
#: deprecated for new users — 404 recommending 3.5).
GEMINI_AV_MODEL_ID: Final = "gemini-3.5-flash-lite"
GEMINI_AV_HOST: Final = "https://generativelanguage.googleapis.com"
GEMINI_AV_API_SURFACE: Final = "google-gemini-developer-api-generate-content-rest-v1beta"

#: Cost rates, USD per 1M tokens, gemini-3.5-flash-lite Standard tier:
#: input text/image/video/audio ALL $0.30, output $2.50.
#: Retrieved 2026-09-10 from https://ai.google.dev/gemini-api/docs/pricing
#: (the previous TEXT=0.10 / OUT=0.40 constants were 2.5-era list prices).
GEMINI_AV_RATES_IN: Final = {"TEXT": 0.30, "AUDIO": 0.30, "IMAGE": 0.30, "VIDEO": 0.30}
GEMINI_AV_RATE_OUT: Final = 2.50

#: Core partition width; context aiding understanding on both sides.
AV_CHUNK_SECONDS: Final = 60.0
AV_CHUNK_CONTEXT_S: Final = 3.0

#: Validation contract version — bump to invalidate stale cached chunk
#: outcomes when the local validation rules change (v2: end-at-duration ok).
AV_VALIDATION_VERSION: Final = "v2"

_CLIP_BOUNDARY_EPSILON: Final = 1e-6

AV_EVENT_FIELDS: Final = (
    "start_seconds",
    "end_seconds",
    "visual",
    "speech",
    "ambient",
    "music",
    "audiovisual_relation",
    "sample_candidate_reason",
)


def gemini_av_prompt(clip_duration_seconds: float) -> str:
    """Trusted observation instructions (proven JA shape from the A/B
    harness, re-scoped to ONE clip: timestamps are clip-local)."""

    return (
        "あなたは映像観察者です。与えられた動画クリップ"
        f"（約{clip_duration_seconds:.0f}秒、音声つき）を最初から最後まで観察し、"  # noqa: RUF001 (proven JA A/B prompt wording)
        "指定のJSONだけを返してください。"
        "編集上の取捨選択・順序変更・評価などの判断は行わないでください（観察のみ）。"  # noqa: RUF001 (proven JA A/B prompt wording)
        "\n\n1. クリップ全体を複数のイベントに分割し、start_seconds の昇順で "
        "events に列挙してください。"
        "\n2. 各イベントについて次のフィールドを必ず埋めてください"
        "（該当なしは空文字列）:"  # noqa: RUF001 (proven JA A/B prompt wording)
        "\n   - visual: 画面に見えている内容の客観的な説明（日本語）"  # noqa: RUF001 (proven JA A/B prompt wording)
        "\n   - speech: 聞こえた発話を日本語でほぼ逐語的に書き取る。"
        "発話がなければ空文字列"
        "\n   - ambient: 環境音・効果音(SE)の有無・性格・タイミング。"
        "なければ空文字列"
        "\n   - music: 音楽(BGM)の有無・性格・変化のタイミング。なければ空文字列"
        "\n   - audiovisual_relation: 音と映像の対応関係"
        "（同期、画面外音、余白など）"  # noqa: RUF001 (proven JA A/B prompt wording)
        "\n   - sample_candidate_reason: このクリップ内で1〜3箇所のみ、"
        "「相談用サンプル」として適切な候補に理由を書く。それ以外は空文字列"
        "\n3. start_seconds / end_seconds はクリップ先頭からの秒数（数値）。"  # noqa: RUF001 (proven JA A/B prompt wording)
        f"必ず 0 以上 {clip_duration_seconds:.2f} 未満の範囲に収めてください。"
        "範囲外の値が1件でもあるとこのクリップの観察は不採用になります。"
        "\n4. notes に全体的な観察メモ、uncertain_flags に不確かな点を書く。"
    )


def gemini_av_schema() -> dict[str, Any]:
    """Wire-projected response schema (plain types only — no $defs)."""

    fields = {
        name: {"type": "number" if name.endswith("_seconds") else "string"}
        for name in AV_EVENT_FIELDS
    }
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": fields,
                    "required": list(AV_EVENT_FIELDS),
                },
            },
            "notes": {"type": "string"},
            "uncertain_flags": {"type": "string"},
        },
        "required": ["events", "notes", "uncertain_flags"],
    }


class GeminiAvEvent(StrictModel):
    """One AV observation event (Gemini speech is observation only)."""

    start_seconds: Second
    end_seconds: Second
    visual: str = ""
    speech: str = ""
    ambient: str = ""
    music: str = ""
    audiovisual_relation: str = ""
    sample_candidate_reason: str = ""


class AvDriftFlag(StrictModel):
    """One event whose raw CLIP-LOCAL timestamps fell outside the clip."""

    index: int
    raw_start: float
    raw_end: float


@dataclass(frozen=True, slots=True)
class AvValidation:
    """Strict verdict: adopted-local events, or insufficient (no rescue)."""

    events: tuple[GeminiAvEvent, ...]
    drift_flags: tuple[AvDriftFlag, ...]
    chunk_insufficient: bool


@dataclass(frozen=True, slots=True)
class AvChunk:
    """One core plus its context-extended clip (all seconds, global)."""

    index: int
    core_start: float
    core_end: float
    clip_start: float
    clip_end: float

    @property
    def clip_duration_seconds(self) -> float:
        return self.clip_end - self.clip_start

    @property
    def clip_offset_seconds(self) -> float:
        return self.clip_start


def extend_core(
    index: int,
    core_start: float,
    core_end: float,
    duration_seconds: float,
    context_seconds: float = AV_CHUNK_CONTEXT_S,
) -> AvChunk:
    """Clip = core extended by context on both sides, clamped to media."""

    return AvChunk(
        index=index,
        core_start=core_start,
        core_end=core_end,
        clip_start=max(0.0, core_start - context_seconds),
        clip_end=min(duration_seconds, core_end + context_seconds),
    )


def partition_av_cores(
    duration_seconds: float,
    chunk_seconds: float = AV_CHUNK_SECONDS,
) -> tuple[AvChunk, ...]:
    """Cores cover ``[0, duration)`` exactly once: no overlap, no gap."""

    if not duration_seconds > 0:
        raise SampleObservationError(
            "sample-observation-unavailable",
            "the edit source reports no duration; cannot partition AV cores",
        )
    chunks: list[AvChunk] = []
    start, index = 0.0, 0
    while start < duration_seconds:
        end = min(start + chunk_seconds, duration_seconds)
        chunks.append(extend_core(index, start, end, duration_seconds))
        start, index = end, index + 1
    return tuple(chunks)


def validate_chunk_events(
    raw_events: object, chunk: AvChunk
) -> AvValidation:
    """Strict-parse CLIP-LOCAL events against ``[0, clip_duration)`` (pure).

    Malformed payloads are a typed failure (the server backfills that
    chunk). ANY out-of-range event marks the chunk insufficient and NO
    event is adopted — never clamped, never rescued, never a candidate.
    """

    clip_duration = chunk.clip_duration_seconds
    if not isinstance(raw_events, list) or not raw_events:
        return AvValidation(events=(), drift_flags=(), chunk_insufficient=True)
    parsed: list[GeminiAvEvent] = []
    flags: list[AvDriftFlag] = []
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise SampleObservationError(
                "sample-av-bad-response",
                "the Gemini AV response is not strict bounded JSON "
                "(provider-controlled text is never echoed)",
            )
        try:
            event = GeminiAvEvent.model_validate(raw)
        except ValueError:
            raise SampleObservationError(
                "sample-av-bad-response",
                "the Gemini AV response is not strict bounded JSON "
                "(provider-controlled text is never echoed)",
            ) from None
        raw_start, raw_end = event.start_seconds, event.end_seconds
        # End-at-clip-duration is the NATURAL last-event boundary (live:
        # 4/5 chunks died solely on end == clip_duration); only genuinely
        # out-of-range timestamps reject. Start stays strictly inside.
        in_range = (
            0.0 <= raw_start < clip_duration
            and 0.0 <= raw_end <= clip_duration + _CLIP_BOUNDARY_EPSILON
            and raw_end >= raw_start
        )
        if not in_range:
            flags.append(
                AvDriftFlag(index=index, raw_start=raw_start, raw_end=raw_end)
            )
        else:
            parsed.append(event)
    if flags:
        return AvValidation(events=(), drift_flags=tuple(flags), chunk_insufficient=True)
    return AvValidation(
        events=tuple(parsed), drift_flags=(), chunk_insufficient=False
    )


def adopt_core_events(
    validation: AvValidation, chunk: AvChunk
) -> tuple[GeminiAvEvent, ...]:
    """Local → global by the clip offset; adopt core starts only.

    Context-region starts are dropped for adoption — the neighboring
    core adopts its own starts, so boundaries never duplicate.
    """

    if validation.chunk_insufficient:
        return ()
    offset = chunk.clip_offset_seconds
    adopted: list[GeminiAvEvent] = []
    for event in validation.events:
        global_start = event.start_seconds + offset
        if chunk.core_start <= global_start < chunk.core_end:
            adopted.append(
                event.model_copy(
                    update={
                        "start_seconds": global_start,
                        "end_seconds": event.end_seconds + offset,
                    }
                )
            )
    return tuple(adopted)


@dataclass(frozen=True, slots=True)
class AvChunkOutcome:
    """One chunk's Gemini result: adopted GLOBAL events + meter (no key)."""

    chunk_index: int
    events: tuple[GeminiAvEvent, ...]
    drift_flags: tuple[AvDriftFlag, ...]
    chunk_insufficient: bool
    notes: str
    uncertain_flags: str
    meter: Mapping[str, Any]
    reused: bool


__all__ = [
    "AV_CHUNK_CONTEXT_S", "AV_CHUNK_SECONDS", "AV_EVENT_FIELDS",
    "GEMINI_AV_API_SURFACE", "GEMINI_AV_HOST", "GEMINI_AV_MODEL_ID",
    "GEMINI_AV_RATES_IN", "GEMINI_AV_RATE_OUT", "AvChunk", "AvChunkOutcome",
    "AvDriftFlag", "AvValidation", "GeminiAvEvent", "adopt_core_events",
    "extend_core", "gemini_av_prompt", "gemini_av_schema", "partition_av_cores",
    "validate_chunk_events",
]
