"""Gemini AV meter, cost, and reuse sidecars (no wire, no validation).

One JSON sidecar (``AV_CALLS_NAME``, the single definition) records each
metered chunk call keyed by (proxy bytes, chunk bounds, model, contract);
a repeat run reuses it without re-billing. Chunk clips upload fresh on a
cache miss (their re-encodes are not byte-stable, so a clip-sha file
sidecar would never hit). Key VALUES never enter any record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from services.foundation_io import atomic_write
from services.media_intelligence.gemini_av_models import (
    AV_VALIDATION_VERSION,
    GEMINI_AV_RATE_OUT,
    GEMINI_AV_RATES_IN,
    AvChunk,
    AvChunkOutcome,
    AvDriftFlag,
    GeminiAvEvent,
    gemini_av_prompt,
    gemini_av_schema,
)

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2

#: Metered-call reuse journal (chunked; same name the whole-video path used).
AV_CALLS_NAME: Final = "gemini-av-calls.json"
#: Uploaded-file sidecar reuse window (kept for the call journal's file_name
#: provenance; chunk clips upload fresh — see gemini_av_wire).
AV_FILE_REUSE_SECONDS: Final = 24.0 * 3600.0


def cost_from_usage(usage: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    """USD split from usageMetadata (per-modality when present)."""

    def _number(value: object) -> float:
        return float(value) if isinstance(value, (int, float)) else 0.0

    output_usd = _number(usage.get("candidatesTokenCount")) / 1e6 * GEMINI_AV_RATE_OUT
    split = [
        (str(detail.get("modality", "")).upper(), _number(detail.get("tokenCount")))
        for detail in (usage.get("promptTokensDetails") or [])
        if isinstance(detail, Mapping)
    ]
    if split:
        input_usd = sum(
            count / 1e6 * GEMINI_AV_RATES_IN.get(modality, GEMINI_AV_RATES_IN["TEXT"])
            for modality, count in split
        )
        return (
            {
                "input_usd": round(input_usd, 6),
                "output_usd": round(output_usd, 6),
                "total_usd": round(input_usd + output_usd, 6),
            },
            "per-modality promptTokensDetails",
        )
    count = _number(usage.get("promptTokenCount"))
    lower = count / 1e6 * GEMINI_AV_RATES_IN["TEXT"]
    upper = count / 1e6 * GEMINI_AV_RATES_IN["AUDIO"]
    return (
        {
            "input_usd_lower": round(lower, 6),
            "input_usd_upper": round(upper, 6),
            "output_usd": round(output_usd, 6),
        },
        "no modality split: text-rate/medium-rate bounds",
    )


def chunk_av_call_key(  # noqa: PLR0913 (call-cache key: one field per slot)
    *,
    proxy_sha256: str,
    chunk_index: int,
    core_start_seconds: float,
    core_end_seconds: float,
    model_id: str,
    prompt_sha256: str,
    schema_sha256: str,
    validation_version: str = "",
) -> str:
    """Stable reuse key: same proxy + bounds + model + contract = no re-bill.

    Keyed on the WHOLE-PROXY bytes (not the re-encoded chunk clip: the
    VideoToolbox chunk encodes are not byte-stable across runs, so a
    clip-sha key would never hit twice). ``validation_version`` busts the
    cache when local validation rules change.
    """

    joined = (
        f"{proxy_sha256}\n{chunk_index}\n{core_start_seconds!r}\n"
        f"{core_end_seconds!r}\n{model_id}\n{prompt_sha256}\n{schema_sha256}"
        f"\n{validation_version}"
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _calls_path(episode_dir: Path) -> Path:
    return episode_dir / "consultation" / AV_CALLS_NAME


def load_cached_av_call(episode_dir: Path, key: str) -> dict[str, Any] | None:
    """A recorded chunk result for the same (proxy, bounds, contract)."""

    path = _calls_path(episode_dir)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    record = document.get(key)
    return record if isinstance(record, dict) else None


def store_av_call(episode_dir: Path, key: str, record: dict[str, Any]) -> None:
    """Persist one metered chunk call (model/latency/tokens/USD — never keys)."""

    path = _calls_path(episode_dir)
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                document = parsed
        except (OSError, ValueError):
            document = {}
    document[key] = record
    atomic_write(
        path,
        (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode(),
    )


def chunk_cache_key(
    *,
    proxy_sha256: str,
    pin: EditorialPinV2,
    chunk: AvChunk,
) -> str:
    """Reuse key for one chunk (prompt/schema hashed; no file read needed)."""

    prompt = gemini_av_prompt(chunk.clip_duration_seconds)
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    schema_sha = hashlib.sha256(
        json.dumps(gemini_av_schema(), sort_keys=True).encode()
    ).hexdigest()
    return chunk_av_call_key(
        proxy_sha256=proxy_sha256,
        chunk_index=chunk.index,
        core_start_seconds=chunk.core_start,
        core_end_seconds=chunk.core_end,
        model_id=pin.model_id,
        prompt_sha256=prompt_sha,
        schema_sha256=schema_sha,
        validation_version=AV_VALIDATION_VERSION,
    )


def chunk_outcome_from_record(
    record: Mapping[str, Any], chunk: AvChunk, *, reused: bool
) -> AvChunkOutcome:
    """Rebuild one chunk's outcome from its journal record (no re-billing)."""

    raw_events = record.get("events")
    events = (
        tuple(GeminiAvEvent.model_validate(item) for item in raw_events)
        if isinstance(raw_events, list)
        else ()
    )
    raw_flags = record.get("drift_flags")
    flags = (
        tuple(AvDriftFlag.model_validate(item) for item in raw_flags)
        if isinstance(raw_flags, list)
        else ()
    )
    meter = record.get("meter", record)
    notes = record.get("notes")
    uncertain = record.get("uncertain_flags")
    insufficient = record.get("chunk_insufficient")
    return AvChunkOutcome(
        chunk_index=chunk.index,
        events=events,
        drift_flags=flags,
        chunk_insufficient=bool(insufficient),
        notes=notes if isinstance(notes, str) else "",
        uncertain_flags=uncertain if isinstance(uncertain, str) else "",
        meter=meter if isinstance(meter, Mapping) else {},
        reused=reused,
    )


def cached_chunk_outcome(
    *,
    episode_dir: Path,
    proxy_sha256: str,
    pin: EditorialPinV2,
    chunk: AvChunk,
) -> AvChunkOutcome | None:
    """The recorded outcome for this (proxy, bounds, contract), if any."""

    key = chunk_cache_key(proxy_sha256=proxy_sha256, pin=pin, chunk=chunk)
    cached = load_cached_av_call(episode_dir, key)
    if cached is None:
        return None
    return chunk_outcome_from_record(cached, chunk, reused=True)


__all__ = [
    "AV_CALLS_NAME",
    "AV_FILE_REUSE_SECONDS",
    "cached_chunk_outcome",
    "chunk_av_call_key",
    "chunk_cache_key",
    "chunk_outcome_from_record",
    "cost_from_usage",
    "load_cached_av_call",
    "store_av_call",
]
