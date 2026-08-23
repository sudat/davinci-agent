"""The env-gated LIVE editorial-v2 transport over stdlib urllib (task 3).

Lives in the CLI layer BY DESIGN, verbatim after ``services/cli/
live_editorial.py``: ``services/**`` is a network-free boundary (the
services-wide AST guard forbids urllib/http/socket outside this layer), so
the one bounded HTTPS POST against the pinned model contract
(``openai-responses-structured-output``) is issued from here. The transport
is DENIED BY DEFAULT and never fabricates: :func:`make_http_post` verifies
the credential env AND the explicit network-enabled flag BEFORE any socket
can open — a gate failure is a typed :class:`EditorialTransportGatedError`
raised at construction time (zero socket by construction). The credential
VALUE is read only into the Authorization header — it never enters
messages, records, logs, or error details. Redirects are refused outright
so the bearer can never ride one.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

from services.editorial_v2.model_provider import (
    EditorialHttpResponseError,
    EditorialRedirectRefusedError,
    EditorialRuntimeError,
)

if TYPE_CHECKING:
    from services.editorial_v2.model_provider import HttpPost

CREDENTIALS_ENV: Final = "EDITORIAL_DIRECTOR_API_KEY"
NETWORK_ENV: Final = "EDITORIAL_DIRECTOR_NETWORK_ENABLED"
DEFAULT_ENDPOINT: Final = "https://api.openai.com/v1/responses"
ALLOWED_HOSTS: Final = frozenset({"api.openai.com"})
ALLOWED_SCHEMES: Final = frozenset({"https"})
REQUEST_TIMEOUT_SECONDS: Final = 120
_ERROR_BODY_LIMIT: Final = 300


class EditorialTransportGatedError(Exception):
    """Env gate unmet — raised BEFORE any socket; never a fabricated response."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _gate(env: Mapping[str, str]) -> None:
    if not env.get(CREDENTIALS_ENV):
        raise EditorialTransportGatedError(
            "no-credentials",
            f"live editorial-v2 transport requires {CREDENTIALS_ENV}; refusing "
            "before any network access",
        )
    if env.get(NETWORK_ENV) != "1":
        raise EditorialTransportGatedError(
            "live-disabled",
            f"{CREDENTIALS_ENV} is set but the explicit network flag {NETWORK_ENV}=1 "
            "is not; refusing before any network access — this keeps every default "
            "environment network-free",
        )


def _require_pinned(url: str) -> None:
    """The exact HTTPS origin must be allowlisted; no credentials in URLs."""

    parsed = urlparse(url)
    if (
        parsed.scheme not in ALLOWED_SCHEMES
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        allowlist = ", ".join(sorted(ALLOWED_HOSTS))
        raise ValueError(
            "endpoint-not-pinned: "
            f"live editorial-v2 endpoint {url!r} is not pinned: the exact HTTPS "
            f"origin must be one of [{allowlist}] (scheme https, exact host, no "
            "credentials in URL)"
        )


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
        raise EditorialRedirectRefusedError(redirect_to)


class _LiveHttpPost:
    """One bounded HTTPS structured-output POST; env-gated, never fabricated."""

    __slots__ = ("_env",)

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes:
        _require_pinned(url)
        merged = dict(headers)
        merged["Authorization"] = f"Bearer {self._env.get(CREDENTIALS_ENV, '')}"
        http_request = urllib.request.Request(  # noqa: S310 (https endpoint, pinned surface)
            url,
            data=body,
            headers=merged,
            method="POST",
        )
        try:
            with urllib.request.build_opener(_PinnedRedirectHandler()).open(
                http_request, timeout=timeout_s
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read()[:_ERROR_BODY_LIMIT].decode("utf-8", "replace")
            raise EditorialHttpResponseError(error.code, detail) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError(
                    f"the pinned editorial endpoint timed out after {timeout_s:.0f}s"
                ) from error
            raise EditorialRuntimeError(
                "production-model-unavailable",
                f"the pinned editorial endpoint is unreachable: {error.reason}",
            ) from error


def make_http_post(env: Mapping[str, str] | None = None) -> HttpPost:
    """Construct the live transport, or refuse at the gate BEFORE any socket.

    ``env=None`` reads the process environment. Requires BOTH
    ``EDITORIAL_DIRECTOR_API_KEY`` and ``EDITORIAL_DIRECTOR_NETWORK_ENABLED=1``
    (one provider account shared by all three editorial pins).
    """

    resolved = dict(os.environ) if env is None else dict(env)
    _gate(resolved)
    return _LiveHttpPost(resolved)


__all__ = [
    "ALLOWED_HOSTS",
    "CREDENTIALS_ENV",
    "DEFAULT_ENDPOINT",
    "NETWORK_ENV",
    "REQUEST_TIMEOUT_SECONDS",
    "EditorialTransportGatedError",
    "make_http_post",
]
