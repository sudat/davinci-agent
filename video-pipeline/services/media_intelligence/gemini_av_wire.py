# allow: SIZE_OK — the generate + single-chunk-call assembly moved verbatim
# (live-verified camelCase/no-thinkingConfig shapes); condensing the typed
# error branches would re-risk the wire.
"""Gemini AV generate wire: one pinned generateContent per chunk.

Moved verbatim from the former whole-video module (wire facts in the
docstring are live-verified, not re-bisected): ``fileData`` is camelCase
and ``thinkingConfig`` is NEVER sent (live-verified 400).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.foundation_io import sha256_file
from services.media_intelligence.gemini_av_files import (
    GeminiAvHttp,
    _is_success,
    _key_headers,
    _transport_error,
    files_poll_active,
    files_upload_proxy,
)
from services.media_intelligence.gemini_av_models import (
    GEMINI_AV_API_SURFACE,
    GEMINI_AV_MODEL_ID,
    AvChunk,
    AvChunkOutcome,
    adopt_core_events,
    gemini_av_prompt,
    gemini_av_schema,
    validate_chunk_events,
)
from services.media_intelligence.gemini_av_record import (
    chunk_cache_key,
    chunk_outcome_from_record,
    cost_from_usage,
    store_av_call,
)
from services.media_intelligence.sample_observation import SampleObservationError

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2


def generate_av_content(
    transport: GeminiAvHttp,
    *,
    api_key: str,
    endpoint: str,
    file_uri: str,
    duration_seconds: float,
) -> dict[str, Any]:
    """ONE generateContent (LOW resolution, strict JSON — no thinkingConfig)."""

    schema = gemini_av_schema()
    body = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": gemini_av_prompt(duration_seconds)},
                        {
                            # fileData/mimeType/fileUri are camelCase on the live wire.
                            "fileData": {"mimeType": "video/mp4", "fileUri": file_uri},
                        },
                    ],
                }
            ],
            "generationConfig": {
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.0,
                # NEVER thinkingConfig: live-verified 400 INVALID_ARGUMENT.
            },
        },
        ensure_ascii=False,
    ).encode()
    started = time.monotonic()
    try:
        response = transport.post(
            endpoint,
            {**_key_headers(api_key), "Content-Type": "application/json"},
            body,
            300.0,
        )
    except Exception as error:
        raise _transport_error("the Gemini generate call", error) from error
    latency_ms = round((time.monotonic() - started) * 1000)
    if not _is_success(response.status):
        raise SampleObservationError(
            "sample-av-provider-error",
            f"Gemini answered HTTP {response.status}; provider body is never echoed",
        )
    try:
        document = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response is not JSON (provider text never echoed)",
        ) from None
    if not isinstance(document, dict):
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response is not JSON (provider text never echoed)",
        )
    document["latency_ms"] = latency_ms
    return document


def candidate_text(document: Mapping[str, Any]) -> str:
    """First candidate's concatenated text parts (strict envelope)."""

    candidates = document.get("candidates")
    first = candidates[0] if isinstance(candidates, list) and candidates else None
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response carries no candidate text",
        )
    texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    joined = "".join(text for text in texts if isinstance(text, str)).strip()
    if not joined:
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response carries no candidate text",
        )
    return joined


def _check_pin(pin: EditorialPinV2, env: Mapping[str, str]) -> str:
    from services.editorial_v2.editorial_pins import (  # noqa: PLC0415 (pin gate stays off the fast path)
        EditorialRuntimeError,
        require_pin_env,
    )

    if pin.api_surface != GEMINI_AV_API_SURFACE:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"pin {pin.purpose} carries api_surface {pin.api_surface!r}; refusing",
        )
    if pin.endpoint is None:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"pin {pin.purpose} declares no endpoint; refusing",
        )
    if pin.model_id != GEMINI_AV_MODEL_ID:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"pin {pin.purpose} names model {pin.model_id!r}; refusing a silent switch",
        )
    try:
        require_pin_env(pin, env)
        return env[pin.external_credentials[0]]
    except (KeyError, IndexError) as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            "the Gemini AV pin credentials are not configured (names only)",
        ) from error
    except EditorialRuntimeError as error:
        raise SampleObservationError(
            "sample-av-unavailable", f"Gemini AV pin or credentials: {error.code}"
        ) from error


def observe_chunk_av(  # noqa: PLR0913 (chunk call contract: one field per wire slot)
    transport: GeminiAvHttp,
    *,
    pin: EditorialPinV2,
    env: Mapping[str, str],
    episode_dir: Path,
    episode_id: str,
    proxy_sha256: str,
    chunk: AvChunk,
    clip_path: Path,
) -> AvChunkOutcome:
    """Upload-once + ONE generateContent for one chunk + validate + meter.

    Exactly one generate call on a cache miss — no retry loops, no rescue:
    an out-of-range event marks the chunk insufficient (the server
    backfills it). The caller checks ``cached_chunk_outcome`` first to
    skip extraction/upload entirely on a hit.
    """

    api_key = _check_pin(pin, env)
    if pin.endpoint is None:  # narrowed by _check_pin; keeps typing exact
        raise SampleObservationError("sample-av-unavailable", "no endpoint")
    key = chunk_cache_key(proxy_sha256=proxy_sha256, pin=pin, chunk=chunk)
    clip_sha = sha256_file(clip_path)
    # Chunk clips always upload fresh on a cache miss: the VideoToolbox
    # chunk re-encodes are not byte-stable across runs, so a clip-sha file
    # sidecar would never hit; the call-cache (proxy+chunk bounds) is reuse.
    file_name = files_upload_proxy(
        transport,
        api_key=api_key,
        proxy_path=clip_path,
        display_name=f"av-chunk-{episode_id}-c{chunk.index}",
    )
    document = generate_av_content(
        transport,
        api_key=api_key,
        endpoint=pin.endpoint,
        file_uri=files_poll_active(transport, api_key=api_key, file_name=file_name),
        duration_seconds=chunk.clip_duration_seconds,
    )
    usage = document.get("usageMetadata")
    usage_map = usage if isinstance(usage, Mapping) else {}
    cost, method = cost_from_usage(usage_map)
    try:
        payload = json.loads(candidate_text(document))
    except ValueError:
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response text is not JSON (never echoed)",
        ) from None
    if not isinstance(payload, dict):
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini AV response text is not JSON (never echoed)",
        )
    validation = validate_chunk_events(payload.get("events"), chunk)
    adopted = adopt_core_events(validation, chunk)
    notes = payload.get("notes")
    uncertain = payload.get("uncertain_flags")
    record: dict[str, Any] = {
        "model": pin.model_id,
        "latency_ms": document.get("latency_ms"),
        "tokens": {
            "prompt": usage_map.get("promptTokenCount"),
            "cached": usage_map.get("cachedContentTokenCount"),
            "output": usage_map.get("candidatesTokenCount"),
            "total": usage_map.get("totalTokenCount"),
            "prompt_modality_details": usage_map.get("promptTokensDetails"),
        },
        "cost_usd": cost,
        "cost_method": method,
        # gemini-3.5-flash-lite Standard, retrieved 2026-09-10
        # from https://ai.google.dev/gemini-api/docs/pricing
        "cost_rates_comment": "USD/1MTok: input 0.30 (text/image/video/audio) + output 2.50",
        "file_name": file_name,
        "clip_sha256": clip_sha,
        "proxy_sha256": proxy_sha256,
        "chunk_index": chunk.index,
        "core_start_seconds": chunk.core_start,
        "core_end_seconds": chunk.core_end,
        "clip_start_seconds": chunk.clip_start,
        "clip_end_seconds": chunk.clip_end,
        "events": [
            {
                "start_seconds": event.start_seconds,
                "end_seconds": event.end_seconds,
                "visual": event.visual,
                "speech": event.speech,
                "ambient": event.ambient,
                "music": event.music,
                "audiovisual_relation": event.audiovisual_relation,
                "sample_candidate_reason": event.sample_candidate_reason,
            }
            for event in adopted
        ],
        "drift_flags": [
            {
                "index": flag.index,
                "raw_start": flag.raw_start,
                "raw_end": flag.raw_end,
            }
            for flag in validation.drift_flags
        ],
        "chunk_insufficient": validation.chunk_insufficient,
        "notes": notes if isinstance(notes, str) else "",
        "uncertain_flags": uncertain if isinstance(uncertain, str) else "",
    }
    store_av_call(episode_dir, key, record)
    return chunk_outcome_from_record(record, chunk, reused=False)


__all__ = [
    "candidate_text",
    "generate_av_content",
    "observe_chunk_av",
]
