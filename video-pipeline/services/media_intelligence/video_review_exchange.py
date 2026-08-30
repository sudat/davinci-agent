"""T4 bounded provider wire exchange: one POST round-trip, typed failures.

Transport failures (timeout, redirect, HTTP status, unknown) are typed and
NEVER retried; the unknown-exception branch exposes a STABLE message only
— exception text is suppressed entirely because it may carry credentials,
paths, or URLs. Empty and oversized responses are transport-shape failures
and also fail immediately: the ONE retry is reserved exclusively for
parser-raised malformed/schema/range failures. The authoritative request
bound is the TOTAL serialized body (:func:`bounded_request`), not any
single payload component.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from services.editorial_v2.editorial_pins import (
    EditorialHttpResponseError,
    EditorialRedirectRefusedError,
    HttpPost,
)
from services.media_intelligence.video_review_wire import VideoProviderError

REQUEST_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_MAX_MEDIA_BYTES: Final = 15 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
#: Total serialized request ceiling (Gemini inline-data total request limit
#: is documented at ~20 MiB; base64 expansion + prompt/schema bytes count).
DEFAULT_MAX_REQUEST_BYTES: Final = 20 * 1024 * 1024
#: One call + ONE retry, parser-raised malformed output only.
_MAX_ATTEMPTS: Final = 2


@dataclass(frozen=True, slots=True)
class WireCall:
    """One bounded provider wire call (a value object, not a framework)."""

    transport: HttpPost
    endpoint: str
    headers: Mapping[str, str]
    body: bytes
    timeout_s: float
    max_response_bytes: int
    what: str


@dataclass(frozen=True, slots=True)
class WireOutcome[ResultT]:
    """A parsed provider result plus the ACTUAL transport attempt count.

    T6 stage lineage consumes this trace so a malformed-then-valid retry
    records ``attempts=2``; transport-shaped failures raise a
    ``VideoProviderError`` whose ``attempts`` is equally truthful.
    """

    payload: ResultT
    attempts: int


def bounded_request(body: bytes, max_request_bytes: int, what: str) -> bytes:
    """The authoritative request bound: total serialized body bytes."""

    if len(body) > max_request_bytes:
        raise VideoProviderError(
            "provider-request-oversize",
            f"the serialized {what} request is {len(body)} bytes, over the "
            f"{max_request_bytes}-byte total bound; shorten the clip window or "
            "the untrusted data",
        )
    return body


def exchange_with_trace[ResultT](
    call: WireCall, parse: Callable[[bytes], ResultT]
) -> WireOutcome[ResultT]:
    """Transport round-trip returning the payload AND the attempt count.

    Typed failures, one parser-only retry — identical semantics to
    :func:`exchange`; every raised ``VideoProviderError`` now carries the
    number of transport calls actually made.
    """

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = call.transport(call.endpoint, call.headers, call.body, call.timeout_s)
        except TimeoutError:
            raise VideoProviderError(
                "provider-timeout",
                f"the pinned {call.what} call exceeded {call.timeout_s:.0f}s",
                attempts=attempt,
            ) from None
        except EditorialRedirectRefusedError:
            raise VideoProviderError(
                "provider-redirect-refused",
                f"the pinned {call.what} endpoint answered a redirect; redirects "
                "are refused and never followed (target suppressed)",
                attempts=attempt,
            ) from None
        except EditorialHttpResponseError as error:
            raise VideoProviderError(
                "provider-http-status",
                f"the pinned {call.what} endpoint answered HTTP {error.status_code}",
                attempts=attempt,
            ) from None
        except Exception:  # noqa: BLE001 (boundary: wrap ANY transport failure typed; text suppressed)
            # Unknown cause: NEVER echo exception text — it may carry
            # credentials, headers, local paths, or URLs.
            raise VideoProviderError(
                "provider-transport",
                f"the pinned {call.what} transport failed; details suppressed "
                "(exception text is never echoed)",
                attempts=attempt,
            ) from None
        if not raw:
            raise VideoProviderError(
                "provider-response-empty",
                f"the pinned {call.what} response body is empty — not model output, never retried",
                attempts=attempt,
            ) from None
        if len(raw) > call.max_response_bytes:
            raise VideoProviderError(
                "provider-response-oversize",
                f"the pinned {call.what} response is {len(raw)} bytes, over the "
                f"{call.max_response_bytes}-byte bound — never retried",
                attempts=attempt,
            ) from None
        try:
            return WireOutcome(payload=parse(raw), attempts=attempt)
        except VideoProviderError as error:
            if attempt == _MAX_ATTEMPTS:
                raise VideoProviderError(
                    error.code,
                    f"{error.detail} — refused after {_MAX_ATTEMPTS - 1} retry "
                    f"({_MAX_ATTEMPTS} attempts)",
                    attempts=attempt,
                ) from None
    raise AssertionError("unreachable: the retry loop returns or raises")


def exchange[ResultT](call: WireCall, parse: Callable[[bytes], ResultT]) -> ResultT:
    """T4-compatible payload-only round-trip (delegates to the trace form)."""

    return exchange_with_trace(call, parse).payload


__all__ = [
    "DEFAULT_MAX_MEDIA_BYTES",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "WireCall",
    "WireOutcome",
    "bounded_request",
    "exchange",
    "exchange_with_trace",
]
