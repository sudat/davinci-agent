"""Blocker-fix regression: live transcript redaction + pinned HTTPS origin.

The live editorial transport used to (a) build the director request from
RAW transcript text, shipping PII/secrets to the cloud prompt, and (b)
accept any EDITORIAL_DIRECTOR_BASE_URL with the bearer attached and
redirect-following enabled. The fixed path redacts every transcript
string before prompt construction, denies live transport when sensitive
content survives redaction, and pins the endpoint to an exact HTTPS host
allowlist with redirects refused.
"""

from __future__ import annotations

import urllib.request

import pytest

from services.cli.live_editorial import (
    ALLOWED_ENDPOINT_HOSTS,
    ENDPOINT_ENV,
    LiveHttpTransport,
    _no_redirect_opener,
    pinned_endpoint,
)
from services.cli.real_director import RealDirectorError, redacted_speech_text
from services.editorial.pin import load_pin
from services.editorial.transport import (
    CREDENTIALS_ENV,
    EditorialTransportFailure,
)
from services.policy.redaction import redact_text


def test_transcript_text_is_redacted_before_prompt_use() -> None:
    raw = (
        "contact me at alice@example.com about api_key=sk-live-123456 "
        "or call 09012345678"
    )
    redacted = redact_text(raw)
    assert "alice@example.com" not in redacted
    assert "sk-live-123456" not in redacted
    assert "09012345678" not in redacted
    assert "[EMAIL_REDACTED]" in redacted
    assert "api_key=[REDACTED]" in redacted


def test_pinned_endpoint_accepts_only_exact_https_allowlisted_hosts() -> None:
    assert pinned_endpoint(None, {}) == "https://api.openai.com/v1/responses"
    assert (
        pinned_endpoint("https://api.openai.com/v1/responses", {})
        == "https://api.openai.com/v1/responses"
    )
    for bad in (
        "http://api.openai.com/v1/responses",
        "https://evil.example/v1/responses",
        "https://api.openai.com.evil.example/v1/responses",
        "https://sub.api.openai.com/v1/responses",
        "file:///etc/passwd",
        "not a url",
    ):
        with pytest.raises(ValueError, match="endpoint-not-pinned"):
            pinned_endpoint(bad, {})


def test_env_endpoint_override_must_be_pinned() -> None:
    with pytest.raises(ValueError, match="endpoint-not-pinned"):
        pinned_endpoint(None, {ENDPOINT_ENV: "https://evil.example/v1/responses"})
    assert "api.openai.com" in ALLOWED_ENDPOINT_HOSTS


def test_transport_denies_sensitive_residual_transcript() -> None:

    speech = {"seg-1": "mail bob at bob@example.com now"}
    cleaned = redacted_speech_text(speech)
    assert cleaned == {"seg-1": redact_text("mail bob at bob@example.com now")}

    bare_key = "sk-AbCdEfGhIjKlMnOpQrStUv" * 2
    with pytest.raises(RealDirectorError) as raised:
        redacted_speech_text({"seg-1": f"leaked bearer {bare_key} in speech"})
    assert raised.value.code == "transcript-redaction-ineffective"


def test_bearer_never_rides_redirects() -> None:

    opener = _no_redirect_opener()
    handlers = [
        type(handler).__name__ for handler in getattr(opener, "handlers", ())
    ]
    assert "_PinnedRedirectHandler" in handlers
    handler = next(
        handler
        for handler in getattr(opener, "handlers", ())
        if type(handler).__name__ == "_PinnedRedirectHandler"
    )
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer secret"},
    )
    with pytest.raises(ValueError, match="redirect-refused"):
        handler.redirect_request(
            request,
            None,  # type: ignore[arg-type]
            "https://api.openai.com/v1/responses",
            301,
            "Moved Permanently",
            None,  # type: ignore[arg-type]
        )


def test_gate_still_refuses_without_credentials_or_flag() -> None:
    transport = LiveHttpTransport(
        request=None,  # type: ignore[arg-type]
        pin=load_pin(),
        env={CREDENTIALS_ENV: "value-never-sent"},
    )
    outcome = transport.send("a" * 64)
    assert isinstance(outcome, EditorialTransportFailure)
    assert outcome.code == "live_disabled"
