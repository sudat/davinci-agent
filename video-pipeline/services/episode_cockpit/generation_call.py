"""Codex-transport image-generation seam (工程4, default OFF).

One bounded image request over the Luna codex-exec transport: the caller
passes the pinned ``-m`` model, an optional same-frame input, and the two
gates (explicit permission, U47 model verification). Every refusal raises
BEFORE any transport contact (0 calls). Unit tests inject a fake
transport — this module never invokes codex itself.

Delivery honest note: ``codex exec`` surfaces only the assistant's final
text, so the production adapter asks the model to return the panel as a
JSON envelope carrying base64 image bytes, then validates the PNG/JPEG
magic before accepting. Until the App Server savedPath is wired, any
non-image reply is a typed ``generation-bad-image`` — never a silent
accept. UsageLimit/rate replies are typed ``generation-usage-limited``
(no retry, no fallback, never bypassed).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple, Protocol

LUNA_TRANSPORT: Final = "codex-exec"
REQUEST_TIMEOUT_SECONDS: Final = 120.0
IDENTITY_PROMPT: Final = (
    "Reply with ONLY the exact model identifier you are running as "
    "(the -m value you were launched with). No other text."
)

_USAGE_LIMIT_MARKS: Final = (
    "usage limit",
    "rate limit",
    "quota",
    "too many requests",
    "capacity",
    "429",
)


class GenerationError(Exception):
    """Typed generation refusal/failure carrying a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class GenerationModelVerification(NamedTuple):
    model_selected: str | None
    verified: bool
    method: str
    reason: str | None


class ImageTransport(Protocol):
    """One image request → raw image bytes (fake-injectable in tests)."""

    def __call__(
        self, prompt: str, input_frame: Path | None, model: str
    ) -> bytes: ...


def is_usage_limit_text(text: str) -> bool:
    lowered = text.lower()
    return any(mark in lowered for mark in _USAGE_LIMIT_MARKS)


def verify_generation_model(
    *,
    transport: str | None,
    model_pin: str | None,
    probe: Callable[[], str] | None = None,
) -> GenerationModelVerification:
    """U47: confirm the actually-selected model BEFORE any image call.

    Verified=True only when the transport is the Luna codex-exec route,
    the pin names a model, AND a live model-echo probe replies with that
    pinned id. Anything else is unverified (0 image calls downstream).
    The echo is model self-report — documented limit, still stronger
    than trusting argv alone since it proves the selected route answers.
    """

    if transport != LUNA_TRANSPORT:
        return GenerationModelVerification(
            model_selected=model_pin, verified=False, method="luna-only",
            reason=(
                f"generation needs the Luna codex-exec transport, not {transport!r}; "
                "no Image API/Web/other-agent fallback"
            ),
        )
    if not model_pin:
        return GenerationModelVerification(
            model_selected=None, verified=False, method="model-pin-missing",
            reason="the model pin names no model; refusing before any image call",
        )
    if probe is None:
        return GenerationModelVerification(
            model_selected=model_pin, verified=False,
            method="model-echo-probe-missing",
            reason="codex exec exposes no model echo without a live preflight; "
            "refusing before any image call",
        )
    try:
        reply = probe()
    except Exception as error:  # noqa: BLE001 (probe failure IS the unverified signal; never raises)
        return GenerationModelVerification(
            model_selected=model_pin, verified=False,
            method="model-echo-probe-failed",
            reason=f"the model-echo preflight failed ({error}); refusing",
        )
    if model_pin.lower() in reply.lower():
        return GenerationModelVerification(
            model_selected=model_pin, verified=True,
            method="model-echo-probe", reason=None,
        )
    head = reply.strip().replace("\n", " ")[:120] or "empty reply"
    return GenerationModelVerification(
        model_selected=model_pin, verified=False, method="model-echo-mismatch",
        reason=f"the selected model did not confirm {model_pin!r} ({head}); refusing",
    )


def decode_panel_image(payload: str) -> bytes:
    """Base64 envelope → validated image bytes (PNG/JPEG magic required)."""

    text = payload.strip()
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except ValueError as error:
            raise GenerationError(
                "generation-bad-image",
                "the model reply is not a usable image envelope",
            ) from error
        raw = body.get("image_base64") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise GenerationError(
                "generation-bad-image",
                "the model reply carries no image_base64 payload",
            )
        text = raw.strip()
    if is_usage_limit_text(text):
        raise GenerationError(
            "generation-usage-limited",
            "the model reports a usage limit; stopping without retry or bypass",
        )
    try:
        blob = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise GenerationError(
            "generation-bad-image",
            "the model reply is not decodable image bytes",
        ) from error
    if blob.startswith((b"\x89PNG", b"\xff\xd8\xff")):
        return blob
    raise GenerationError(
        "generation-bad-image",
        "decoded bytes are not a PNG/JPEG image; discarding",
    )


def codex_image_transport(
    runner: Callable[..., str],
    *,
    timeout_s: float = REQUEST_TIMEOUT_SECONDS,
) -> ImageTransport:
    """Adapt a codex-exec text runner to the image seam (base64 delivery)."""

    def generate(prompt: str, input_frame: Path | None, model: str) -> bytes:
        images = (input_frame,) if input_frame is not None else ()
        try:
            message = runner(
                prompt, model=model, images=images, timeout_s=timeout_s
            )
        except Exception as error:
            if is_usage_limit_text(str(error)):
                raise GenerationError(
                    "generation-usage-limited",
                    f"the transport reports a usage limit ({error}); stopping",
                ) from error
            raise GenerationError(
                "generation-transport-failed", f"the codex exec call failed: {error}"
            ) from error
        return decode_panel_image(message)

    return generate


def request_panel_image(  # noqa: PLR0913 (explicit single-panel inputs; kwargs are the seam contract)
    *,
    prompt: str,
    input_frame: Path | None,
    model: str,
    permission_granted: bool,
    model_verified: bool,
    transport: ImageTransport | None,
    ensure_budget: Callable[[], None] | None = None,
) -> bytes:
    """One panel's image bytes, or a typed refusal BEFORE any transport call."""

    if not permission_granted:
        raise GenerationError(
            "generation-permission-required",
            "no explicit per-request generation permission; text-only",
        )
    if not model_verified:
        raise GenerationError(
            "generation-model-unverified",
            "the Luna model selection is unconfirmed; 0 image calls",
        )
    if transport is None:
        raise GenerationError(
            "generation-transport-gated",
            "the codex transport is unavailable; generation stays off",
        )
    if ensure_budget is not None:
        ensure_budget()
    try:
        return transport(prompt, input_frame, model)
    except GenerationError:
        raise
    except Exception as error:
        if is_usage_limit_text(str(error)):
            raise GenerationError(
                "generation-usage-limited",
                f"the transport reports a usage limit ({error}); stopping",
            ) from error
        raise GenerationError(
            "generation-failed", f"the image request failed: {error}"
        ) from error


__all__ = [
    "IDENTITY_PROMPT",
    "LUNA_TRANSPORT",
    "REQUEST_TIMEOUT_SECONDS",
    "GenerationError",
    "GenerationModelVerification",
    "ImageTransport",
    "codex_image_transport",
    "decode_panel_image",
    "is_usage_limit_text",
    "request_panel_image",
    "verify_generation_model",
]
