"""The env-gated LIVE editorial transport over stdlib urllib (Todo 46).

Lives in the CLI layer BY DESIGN: ``services/editorial`` is a frozen
network-free boundary (its AST guard forbids http/urllib/socket), so the one
bounded (120 s) HTTPS POST against the pinned model contract
(``openai-responses-structured-output``) is issued from here. The transport
is DENIED BY DEFAULT and never fabricates: it proceeds only when the
credential env AND an explicit network-enabled flag are both set; anything
less is an honest :class:`EditorialTransportFailure` BEFORE any socket
opens. The credential VALUE is read only into the Authorization header — it
never enters messages, records, logs, or error details.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

from services.contracts.editorial_model import EditorialSelectionProposal
from services.editorial.prompt import SYSTEM_PROMPT, UNTRUSTED_DATA_NOTICE, build_prompt
from services.editorial.transport import (
    CREDENTIALS_ENV,
    EditorialOutcome,
    EditorialStrictResponse,
    EditorialTransport,
    EditorialTransportFailure,
)
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.editorial.models import DirectorRequest
    from services.editorial.pin import EditorialDirectorPin

NETWORK_ENV: Final = "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
DEFAULT_ENDPOINT: Final = "https://api.openai.com/v1/responses"
ENDPOINT_ENV: Final = "EDITORIAL_DIRECTOR_BASE_URL"
ALLOWED_ENDPOINT_HOSTS: Final = frozenset({"api.openai.com"})
ALLOWED_ENDPOINT_SCHEMES: Final = frozenset({"https"})
REQUEST_TIMEOUT_SECONDS: Final = 120
_ERROR_BODY_LIMIT: Final = 300

_TRANSPORT_DISABLED_DETAIL = (
    "live editorial HTTP transport requires BOTH {credentials} and {network}=1; "
    "refusing before any network access — unset credentials select the honest "
    "deterministic-baseline director instead"
)


def _gate(env: dict[str, str]) -> EditorialTransportFailure | None:
    if not env.get(CREDENTIALS_ENV):
        return EditorialTransportFailure(
            code="no_credentials",
            detail=_TRANSPORT_DISABLED_DETAIL.format(
                credentials=CREDENTIALS_ENV, network=NETWORK_ENV
            ),
        )
    if env.get(NETWORK_ENV) != "1":
        return EditorialTransportFailure(
            code="live_disabled",
            detail=(
                f"{CREDENTIALS_ENV} is set but the explicit network flag "
                f"{NETWORK_ENV}=1 is not; refusing before any network access — "
                "this keeps every default environment network-free"
            ),
        )
    return None


def pinned_endpoint(endpoint: str | None, env: dict[str, str]) -> str:
    """Resolve the endpoint to an EXACT allowlisted HTTPS origin.

    The override env var may only name an already-pinned host over HTTPS;
    anything else (plain HTTP, another host, a subdomain look-alike, a
    non-http scheme) is refused before any socket opens.
    """
    resolved = endpoint or env.get(ENDPOINT_ENV, DEFAULT_ENDPOINT)
    parsed = urlparse(resolved)
    if (
        parsed.scheme not in ALLOWED_ENDPOINT_SCHEMES
        or parsed.hostname not in ALLOWED_ENDPOINT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        allowlist = ", ".join(sorted(ALLOWED_ENDPOINT_HOSTS))
        raise ValueError(
            "endpoint-not-pinned: "
            f"live editorial endpoint {resolved!r} is not pinned: the exact "
            f"HTTPS origin must be one of [{allowlist}] "
            f"(scheme https, exact host, no credentials in URL)"
        )
    return resolved


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects are refused outright: the bearer must never ride one."""

    def redirect_request(  # type: ignore[override] (refusal contract)  # noqa: PLR0913, PLR0917
        self,
        req: urllib.request.Request,
        fp: object,
        redirect_to: str,
        code: int,
        message: str,
        data: object,
        headers: object = None,
    ) -> urllib.request.Request | None:
        del req, fp, code, message, data, headers
        raise ValueError(
            "redirect-refused: the pinned editorial endpoint answered a "
            f"redirect to {redirect_to!r}; redirects are refused so the "
            "bearer credential can never be replayed to another origin"
        )


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_PinnedRedirectHandler())


def _request_body(pin: EditorialDirectorPin, request: DirectorRequest) -> bytes:
    bundle = build_prompt(request)
    system = f"{SYSTEM_PROMPT}\n\n{UNTRUSTED_DATA_NOTICE}"
    payload = {
        "model": pin.model_id,
        "input": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": canonical_model_bytes(bundle).decode("utf-8"),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": pin.structured_output_schema_id,
                "strict": True,
                "schema": EditorialSelectionProposal.model_json_schema(),
            }
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _extract_output_text(document: object) -> str | None:
    if not isinstance(document, dict):
        return None
    text = document.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    output = document.get("output")
    if not isinstance(output, list):
        return None
    contents = [
        content
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "message"
        and isinstance(content := item.get("content"), list)
    ]
    chunks: list[str] = [
        part["text"]
        for content in contents
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    ]
    joined = "".join(chunks).strip()
    return joined or None


class LiveHttpTransport(EditorialTransport):
    """One bounded HTTPS structured-output call; env-gated, never fabricated."""

    def __init__(
        self,
        *,
        request: DirectorRequest,
        pin: EditorialDirectorPin,
        env: dict[str, str] | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._request = request
        self._pin = pin
        self._env = env if env is not None else dict(os.environ)
        self._endpoint = endpoint

    def send(self, request_hash: str) -> EditorialOutcome:  # noqa: PLR0911 (typed outcome per failure)
        del request_hash
        refused = _gate(self._env)
        if refused is not None:
            return refused
        try:
            endpoint = pinned_endpoint(self._endpoint, self._env)
        except ValueError as error:
            code, _, detail = str(error).partition(": ")
            return EditorialTransportFailure(code=code, detail=detail)
        body = _request_body(self._pin, self._request)
        http_request = urllib.request.Request(  # noqa: S310 (https endpoint, pinned surface)
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._env.get(CREDENTIALS_ENV, '')}",
            },
            method="POST",
        )
        try:
            with _no_redirect_opener().open(
                http_request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw = response.read()
        except ValueError as error:
            code, _, detail = str(error).partition(": ")
            return EditorialTransportFailure(code=code, detail=detail)
        except urllib.error.HTTPError as error:
            detail = error.read()[:_ERROR_BODY_LIMIT].decode("utf-8", "replace")
            return EditorialTransportFailure(
                code="http_status",
                detail=f"the pinned editorial endpoint answered HTTP {error.code}: {detail}",
            )
        except urllib.error.URLError as error:
            return EditorialTransportFailure(
                code="http_unreachable",
                detail=f"the pinned editorial endpoint is unreachable: {error.reason}",
            )
        except TimeoutError:
            return EditorialTransportFailure(
                code="http_timeout",
                detail=f"the pinned editorial call exceeded {REQUEST_TIMEOUT_SECONDS}s",
            )
        try:
            text = _extract_output_text(json.loads(raw.decode("utf-8")))
            if text is None:
                failure = "the model response carries no structured output text"
            else:
                return EditorialStrictResponse(
                    payload=text.encode("utf-8"), served_by=f"live:{self._pin.model_id}"
                )
        except (ValueError, UnicodeDecodeError) as error:
            failure = f"unparseable model response: {error}"
        return EditorialTransportFailure(code="bad_response", detail=failure)


__all__ = [
    "ALLOWED_ENDPOINT_HOSTS",
    "DEFAULT_ENDPOINT",
    "ENDPOINT_ENV",
    "NETWORK_ENV",
    "LiveHttpTransport",
    "pinned_endpoint",
]
