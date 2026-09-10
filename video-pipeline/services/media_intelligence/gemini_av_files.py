"""Gemini Files API transport: resumable upload/poll plus the shared HTTP seam.

Moved verbatim from the former whole-video module (wire facts in the
docstring are live-verified, not re-bisected): the Files API resumable
START answers EMPTY with the upload URL in the ``X-Goog-Upload-URL``
header. Transports are injected (CLI owns sockets); API-key VALUES never
enter records or error details — env-var NAMES only. Transcripts and
policy text are untrusted DATA, never instructions.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from services.media_intelligence.gemini_av_models import GEMINI_AV_HOST
from services.media_intelligence.sample_observation import SampleObservationError

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


__all__ = [
    "AV_FILE_POLL_INTERVAL_S",
    "AV_FILE_POLL_TIMEOUT_S",
    "GeminiAvHttp",
    "GeminiAvHttpResponse",
    "files_poll_active",
    "files_upload_proxy",
]
