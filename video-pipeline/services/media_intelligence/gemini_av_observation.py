"""Gemini whole-video audiovisual observation — ONE metered call (PRD v4.4 §8.5).

The PRIMARY AV path for speech/sound-relevant episodes: a low-res A/V
proxy (480p + AAC, built locally from the Edit Source) rides the Files
API once, then ONE ``generateContent`` returns structured events
[{start_seconds, end_seconds, visual, speech, ambient, music,
audiovisual_relation, sample_candidate_reason}]. Gemini speech NEVER
becomes subtitle authority (ASR remains); no model writes Job State,
Selection Plan, Edit Plan, or Resolve.

Live-verified wire facts (2026-09-11 A/B, embedded so nobody re-bisects):

- model ``gemini-3.5-flash-lite`` (``gemini-2.5-flash-lite`` is deprecated
  for new users — 404 recommending 3.5).
- NEVER send ``thinkingConfig`` — live-verified 400 INVALID_ARGUMENT.
- Files API resumable START response body is EMPTY — the upload URL rides
  the ``X-Goog-Upload-URL`` response header.
- ``fileData`` part uses camelCase (``fileData``/``mimeType``/``fileUri``).
- 282s 480p h264+aac proxy = 25,662 VIDEO tokens ≈ $0.008/call at LOW
  resolution, ~5.6s latency.
- Tail timestamps drift out of bounds on some calls (content still real):
  every event is clamped to ``[0, duration)`` and drift is flagged in the
  observation record — never a whole-call failure for it.

Transports are injected (CLI layer owns sockets); API-key VALUES never
enter messages, records, or error details — env-var NAMES only.
Transcripts and policy text are untrusted DATA, never instructions.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol

from pydantic import BeforeValidator

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, sha256_file
from services.media_intelligence.sample_observation import SampleObservationError

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2

#: Live-verified primary AV model (see module docstring).
GEMINI_AV_MODEL_ID: Final = "gemini-3.5-flash-lite"
GEMINI_AV_HOST: Final = "https://generativelanguage.googleapis.com"
GEMINI_AV_API_SURFACE: Final = "google-gemini-developer-api-generate-content-rest-v1beta"

#: Cost rates, USD per 1M tokens (public list prices, <=200k context).
GEMINI_AV_RATES_IN: Final = {"TEXT": 0.10, "AUDIO": 0.30, "IMAGE": 0.30, "VIDEO": 0.30}
GEMINI_AV_RATE_OUT: Final = 0.40

#: Drift tolerance: >30% of events out of bounds marks the observation
#: route-quality-insufficient (known Gemini tail behavior).
AV_DRIFT_INSUFFICIENT_RATIO: Final = 0.30

#: Uploaded-file sidecar reuse window (Files API TTL is 48h; stay inside it).
AV_FILE_REUSE_SECONDS: Final = 24.0 * 3600.0
AV_FILE_POLL_TIMEOUT_S: Final = 300.0
AV_FILE_POLL_INTERVAL_S: Final = 3.0

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


def gemini_av_prompt(duration_seconds: float) -> str:
    """Trusted observation instructions (proven shape from the A/B harness)."""

    return (
        "あなたは映像観察者です。与えられた動画"
        f"（約{duration_seconds:.0f}秒、音声つき）を最初から最後まで観察し、"  # noqa: RUF001 (proven JA A/B prompt wording)
        "指定のJSONだけを返してください。"
        "編集上の取捨選択・順序変更・評価などの判断は行わないでください（観察のみ）。"  # noqa: RUF001 (proven JA A/B prompt wording)
        "\n\n1. 動画全体を複数のイベントに分割し、start_seconds の昇順で "
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
        "\n   - sample_candidate_reason: 動画全体で3〜8箇所のみ、"
        "「相談用サンプル」として適切な候補に理由を書く。それ以外は空文字列"
        "\n3. start_seconds / end_seconds は動画先頭からの秒数（数値）。"  # noqa: RUF001 (proven JA A/B prompt wording)
        f"必ず 0 以上 {duration_seconds:.2f} 以下の範囲に収めてください。"
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


def _to_float(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


type AvSecond = Annotated[float, BeforeValidator(_to_float)]


class GeminiAvEvent(StrictModel):
    """One clamped AV observation event (Gemini speech is observation only)."""

    start_seconds: AvSecond
    end_seconds: AvSecond
    visual: str = ""
    speech: str = ""
    ambient: str = ""
    music: str = ""
    audiovisual_relation: str = ""
    sample_candidate_reason: str = ""


class AvDriftFlag(StrictModel):
    """One event whose raw timestamps fell outside ``[0, duration)``."""

    index: int
    raw_start: float
    raw_end: float


@dataclass(frozen=True, slots=True)
class AvValidation:
    """Clamped events + drift flags + the insufficient verdict."""

    events: tuple[GeminiAvEvent, ...]
    drift_flags: tuple[AvDriftFlag, ...]
    route_quality_insufficient: bool


def validate_av_events(raw_events: object, duration_seconds: float) -> AvValidation:
    """Strict-parse, clamp to ``[0, duration)``, flag drift (pure).

    Out-of-bounds events are clamped (never dropped, never fatal); the
    observation is route-quality-insufficient when empty or when more
    than 30% of events drifted. Malformed payloads are a typed failure
    whose message carries NO provider text.
    """

    if not isinstance(raw_events, list) or not raw_events:
        return AvValidation(events=(), drift_flags=(), route_quality_insufficient=True)
    events: list[GeminiAvEvent] = []
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
        drifted = not (0.0 <= raw_start <= duration_seconds and 0.0 <= raw_end <= duration_seconds)
        clamped_start = min(max(raw_start, 0.0), duration_seconds)
        clamped_end = min(max(raw_end, 0.0), duration_seconds)
        if clamped_end < clamped_start:
            clamped_end = clamped_start
            drifted = True
        if drifted:
            flags.append(
                AvDriftFlag(index=index, raw_start=raw_start, raw_end=raw_end)
            )
        events.append(
            event.model_copy(
                update={"start_seconds": clamped_start, "end_seconds": clamped_end}
            )
        )
    insufficient = len(flags) / len(events) > AV_DRIFT_INSUFFICIENT_RATIO
    return AvValidation(
        events=tuple(events),
        drift_flags=tuple(flags),
        route_quality_insufficient=insufficient,
    )


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


# ---------------------------------------------------------------------------
# Content-hash reuse (simple JSON sidecar, no cache framework)
# ---------------------------------------------------------------------------

AV_CALLS_NAME: Final = "gemini-av-calls.json"
AV_FILE_SIDECAR_NAME: Final = "gemini-av-file.json"

_HTTP_SUCCESS_MIN: Final = 200
_HTTP_SUCCESS_END: Final = 300


def _is_success(status: int) -> bool:
    return _HTTP_SUCCESS_MIN <= status < _HTTP_SUCCESS_END


def av_call_key(
    *, file_sha256: str, model_id: str, prompt_sha256: str, schema_sha256: str
) -> str:
    """Stable reuse key: same bytes + model + contract = no re-billing."""

    joined = f"{file_sha256}\n{model_id}\n{prompt_sha256}\n{schema_sha256}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _calls_path(episode_dir: Path) -> Path:
    return episode_dir / "consultation" / AV_CALLS_NAME


def load_cached_av_call(episode_dir: Path, key: str) -> dict[str, Any] | None:
    """A recorded result for the same (file, model, contract), if any."""

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
    """Persist one metered call (model/latency/tokens/USD/file — never the key)."""

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


# ---------------------------------------------------------------------------
# Injected transport (CLI owns sockets; this module owns the wire shape)
# ---------------------------------------------------------------------------


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
    """Resumable Files API upload → the ``files/...`` resource name.

    The START response body is EMPTY — the upload URL rides the
    ``X-Goog-Upload-URL`` response header (live-verified).
    """

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


@dataclass(frozen=True, slots=True)
class AvObservation:
    """One whole-video AV observation: clamped events + meter (no key)."""

    events: tuple[GeminiAvEvent, ...]
    drift_flags: tuple[AvDriftFlag, ...]
    route_quality_insufficient: bool
    notes: str
    uncertain_flags: str
    meter: Mapping[str, Any]
    reused: bool


def observe_whole_video_av(  # noqa: PLR0913 (observation call contract: one field per wire slot)
    transport: GeminiAvHttp,
    *,
    pin: EditorialPinV2,
    env: Mapping[str, str],
    episode_dir: Path,
    episode_id: str,
    proxy_path: Path,
    duration_seconds: float,
) -> AvObservation:
    """Upload-once + ONE generateContent + validate/clamp + meter + reuse.

    A recorded result for the same (proxy bytes, model, prompt+schema)
    is reused without re-billing. Exactly one generate call happens on a
    miss — no retry loops, no chunked fallback (insufficient is recorded,
    never retried here).
    """

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
        api_key = env[pin.external_credentials[0]]
    except (KeyError, IndexError) as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            "the Gemini AV pin credentials are not configured (names only)",
        ) from error
    except EditorialRuntimeError as error:
        raise SampleObservationError(
            "sample-av-unavailable", f"Gemini AV pin or credentials: {error.code}"
        ) from error

    file_sha = sha256_file(proxy_path)
    prompt = gemini_av_prompt(duration_seconds)
    schema = gemini_av_schema()
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    schema_sha = hashlib.sha256(
        json.dumps(schema, sort_keys=True).encode()
    ).hexdigest()
    key = av_call_key(
        file_sha256=file_sha,
        model_id=pin.model_id,
        prompt_sha256=prompt_sha,
        schema_sha256=schema_sha,
    )
    cached = load_cached_av_call(episode_dir, key)
    if cached is not None:
        return _observation_from_record(cached, reused=True)

    file_name = _fresh_or_uploaded_file(
        transport,
        api_key=api_key,
        episode_dir=episode_dir,
        proxy_path=proxy_path,
        file_sha=file_sha,
        episode_id=episode_id,
    )
    document = generate_av_content(
        transport,
        api_key=api_key,
        endpoint=pin.endpoint,
        file_uri=files_poll_active(transport, api_key=api_key, file_name=file_name),
        duration_seconds=duration_seconds,
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
    validation = validate_av_events(payload.get("events"), duration_seconds)
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
        "cost_rates_comment": (
            "USD/1MTok: text_in 0.10, audio_in 0.30, media_in 0.30, out 0.40"
        ),
        "file_name": file_name,
        "file_sha256": file_sha,
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
            for event in validation.events
        ],
        "drift_flags": [
            {
                "index": flag.index,
                "raw_start": flag.raw_start,
                "raw_end": flag.raw_end,
            }
            for flag in validation.drift_flags
        ],
        "route_quality_insufficient": validation.route_quality_insufficient,
        "notes": notes if isinstance(notes, str) else "",
        "uncertain_flags": uncertain if isinstance(uncertain, str) else "",
    }
    store_av_call(episode_dir, key, record)
    return _observation_from_record(record, reused=False)


def _observation_from_record(record: Mapping[str, Any], *, reused: bool) -> AvObservation:
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
    insufficient = record.get("route_quality_insufficient")
    return AvObservation(
        events=events,
        drift_flags=flags,
        route_quality_insufficient=bool(insufficient),
        notes=notes if isinstance(notes, str) else "",
        uncertain_flags=uncertain if isinstance(uncertain, str) else "",
        meter=meter if isinstance(meter, Mapping) else {},
        reused=reused,
    )


def _reuse_window_open(document: dict[str, Any]) -> bool:
    try:
        stored_at = float(document.get("stored_at", 0.0))
    except (TypeError, ValueError):
        return False
    return time.time() - stored_at < AV_FILE_REUSE_SECONDS


def _still_active(transport: GeminiAvHttp, api_key: str, file_name: str) -> bool:
    """Best-effort uploaded-file reuse check (any failure → fresh upload)."""

    try:
        response = transport.get(
            f"{GEMINI_AV_HOST}/v1beta/{file_name}", _key_headers(api_key), 60.0
        )
        if not _is_success(response.status):
            return False
        state = json.loads(response.body.decode("utf-8"))
    except Exception:  # noqa: BLE001 (stale-upload reuse is best-effort; a fresh upload follows)
        return False
    return isinstance(state, dict) and state.get("state") == "ACTIVE"


def _fresh_or_uploaded_file(  # noqa: PLR0913 (upload-reuse contract: one field per slot)
    transport: GeminiAvHttp,
    *,
    api_key: str,
    episode_dir: Path,
    proxy_path: Path,
    file_sha: str,
    episode_id: str,
) -> str:
    """Reuse a fresh-enough uploaded file, else upload (no re-upload)."""

    sidecar = episode_dir / "consultation" / AV_FILE_SIDECAR_NAME
    if sidecar.is_file():
        try:
            document = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = None
        if (
            isinstance(document, dict)
            and document.get("proxy_sha256") == file_sha
            and isinstance(document.get("name"), str)
            and _reuse_window_open(document)
            and _still_active(transport, api_key, str(document["name"]))
        ):
            return str(document["name"])
    name = files_upload_proxy(
        transport,
        api_key=api_key,
        proxy_path=proxy_path,
        display_name=f"av-proxy-{episode_id}",
    )
    atomic_write(
        sidecar,
        (
            json.dumps(
                {
                    "name": name,
                    "proxy_sha256": file_sha,
                    "stored_at": time.time(),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    return name


__all__ = [
    "AV_DRIFT_INSUFFICIENT_RATIO",
    "AV_EVENT_FIELDS",
    "GEMINI_AV_API_SURFACE",
    "GEMINI_AV_HOST",
    "GEMINI_AV_MODEL_ID",
    "AvDriftFlag",
    "AvObservation",
    "AvValidation",
    "GeminiAvEvent",
    "GeminiAvHttp",
    "GeminiAvHttpResponse",
    "av_call_key",
    "candidate_text",
    "cost_from_usage",
    "files_poll_active",
    "files_upload_proxy",
    "gemini_av_prompt",
    "gemini_av_schema",
    "generate_av_content",
    "load_cached_av_call",
    "observe_whole_video_av",
    "store_av_call",
    "validate_av_events",
]
