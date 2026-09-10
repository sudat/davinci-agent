# allow: SIZE_OK — the Gemini Files/generate transport moved verbatim
# (live-verified header/camelCase/no-thinkingConfig shapes) plus its single
# chunk-call assembly; condensing the typed error branches would re-risk
# the wire, and splitting the call from its transport would scatter it.
"""Gemini AV transport: upload/poll/generate plus one chunk call.

Moved verbatim from the former whole-video module (wire facts in the
docstring are live-verified, not re-bisected): the Files API resumable
START answers EMPTY with the upload URL in the ``X-Goog-Upload-URL``
header, ``fileData`` is camelCase, and ``thinkingConfig`` is NEVER sent
(live-verified 400). Transports are injected (CLI owns sockets); API-key
VALUES never enter records or error details — env-var NAMES only.
Transcripts and policy text are untrusted DATA, never instructions.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from services.foundation_io import sha256_file
from services.media_intelligence.gemini_av_models import (
    AV_VALIDATION_VERSION,
    GEMINI_AV_API_SURFACE,
    GEMINI_AV_HOST,
    GEMINI_AV_MODEL_ID,
    AvChunk,
    AvChunkOutcome,
    AvDriftFlag,
    GeminiAvEvent,
    adopt_core_events,
    gemini_av_prompt,
    gemini_av_schema,
    validate_chunk_events,
)
from services.media_intelligence.gemini_av_record import (
    chunk_av_call_key,
    cost_from_usage,
    load_cached_av_call,
    store_av_call,
)
from services.media_intelligence.sample_observation import SampleObservationError

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2

AV_FILE_POLL_TIMEOUT_S: Final = 300.0
AV_FILE_POLL_INTERVAL_S: Final = 3.0

_HTTP_SUCCESS_MIN: Final = 200
_HTTP_SUCCESS_END: Final = 300


def _is_success(status: int) -> bool:
    return _HTTP_SUCCESS_MIN <= status < _HTTP_SUCCESS_END


@dataclass(frozen=True, slots=True)
class GeminiAvHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class GeminiAvHttp(Protocol):
    """Header-capable HTTP (the Files API START answers via header)."""

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> GeminiAvHttpResponse: ...

    def get(
        self, url: str, headers: Mapping[str, str], timeout_s: float
    ) -> GeminiAvHttpResponse: ...


def _key_headers(api_key: str) -> dict[str, str]:
    return {"X-Goog-Api-Key": api_key}


def _transport_error(what: str, error: Exception) -> SampleObservationError:
    """Map transport failures to typed errors (provider text never echoed)."""

    from services.editorial_v2.editorial_pins import (  # noqa: PLC0415 (error vocabulary only)
        EditorialHttpResponseError,
        EditorialRuntimeError,
    )
    from services.media_intelligence.video_review_wire import (  # noqa: PLC0415 (error vocabulary only)
        VideoProviderError,
    )

    if isinstance(error, SampleObservationError):
        return error
    if isinstance(error, VideoProviderError):
        return SampleObservationError("sample-av-provider-error", f"{what}: withheld")
    if isinstance(error, EditorialHttpResponseError):
        return SampleObservationError(
            "sample-av-provider-error",
            f"{what} answered HTTP {error.status_code}; provider body is never echoed",
        )
    if isinstance(error, TimeoutError):
        return SampleObservationError(
            "sample-av-timeout",
            f"{what} timed out; refusing (no retry loop)",
        )
    if isinstance(error, EditorialRuntimeError):
        return SampleObservationError(
            "sample-av-transport-unavailable",
            f"{what} is unreachable (detail withheld)",
        )
    return SampleObservationError(
        "sample-av-transport-unknown",
        f"{what} failed ({type(error).__name__}); no detail is echoed",
    )


def files_upload_proxy(
    transport: GeminiAvHttp,
    *,
    api_key: str,
    proxy_path: Path,
    display_name: str,
) -> str:
    """Resumable Files API upload → the ``files/...`` resource name."""

    size = proxy_path.stat().st_size
    try:
        start = transport.post(
            f"{GEMINI_AV_HOST}/upload/v1beta/files",
            {
                **_key_headers(api_key),
                "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
            },
            json.dumps({"file": {"display_name": display_name}}).encode(),
            120.0,
        )
    except Exception as error:
        raise _transport_error("the Gemini Files API start", error) from error
    if not _is_success(start.status):
        raise SampleObservationError(
            "sample-av-provider-error",
            f"the Gemini Files API start answered HTTP {start.status}; "
            "provider body is never echoed",
        )
    upload_url = next(
        (
            value
            for name, value in start.headers.items()
            if name.lower() == "x-goog-upload-url"
        ),
        None,
    )
    if not upload_url:
        raise SampleObservationError(
            "sample-av-provider-error",
            "the Gemini Files API start answered without an upload URL",
        )
    payload = proxy_path.read_bytes()
    try:
        done = transport.post(
            upload_url,
            {
                **_key_headers(api_key),
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Length": str(len(payload)),
            },
            payload,
            300.0,
        )
    except Exception as error:
        raise _transport_error("the Gemini Files API upload", error) from error
    if not _is_success(done.status):
        raise SampleObservationError(
            "sample-av-provider-error",
            f"the Gemini Files API upload answered HTTP {done.status}; "
            "provider body is never echoed",
        )
    try:
        document = json.loads(done.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini Files API upload answer is not JSON",
        ) from None
    file_doc = document.get("file") if isinstance(document, dict) else None
    file_doc = file_doc if isinstance(file_doc, dict) else document
    name = file_doc.get("name") if isinstance(file_doc, dict) else None
    if not isinstance(name, str) or not name:
        raise SampleObservationError(
            "sample-av-bad-response",
            "the Gemini Files API upload answer carries no file name",
        )
    return name


def files_poll_active(
    transport: GeminiAvHttp, *, api_key: str, file_name: str
) -> str:
    """Poll the file resource until ACTIVE → its download URI."""

    deadline = time.monotonic() + AV_FILE_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            response = transport.get(
                f"{GEMINI_AV_HOST}/v1beta/{file_name}", _key_headers(api_key), 60.0
            )
        except Exception as error:
            raise _transport_error("the Gemini Files API poll", error) from error
        if not _is_success(response.status):
            raise SampleObservationError(
                "sample-av-provider-error",
                f"the Gemini Files API poll answered HTTP {response.status}; "
                "provider body is never echoed",
            )
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SampleObservationError(
                "sample-av-bad-response",
                "the Gemini Files API poll answer is not JSON",
            ) from None
        if not isinstance(document, dict):
            raise SampleObservationError(
                "sample-av-bad-response",
                "the Gemini Files API poll answer is not JSON",
            )
        state = document.get("state")
        if state == "ACTIVE":
            uri = document.get("uri")
            if not isinstance(uri, str) or not uri:
                raise SampleObservationError(
                    "sample-av-bad-response",
                    "the Gemini file turned ACTIVE without a URI",
                )
            return uri
        if state == "FAILED":
            raise SampleObservationError(
                "sample-av-provider-error",
                "the Gemini file processing failed; provider detail is never echoed",
            )
        time.sleep(AV_FILE_POLL_INTERVAL_S)
    raise SampleObservationError(
        "sample-av-timeout",
        "the Gemini file did not turn ACTIVE in time; refusing (no retry loop)",
    )


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
    "AV_FILE_POLL_INTERVAL_S", "AV_FILE_POLL_TIMEOUT_S", "GeminiAvHttp",
    "GeminiAvHttpResponse", "cached_chunk_outcome", "candidate_text",
    "chunk_cache_key", "chunk_outcome_from_record", "files_poll_active",
    "files_upload_proxy", "generate_av_content", "observe_chunk_av",
]
