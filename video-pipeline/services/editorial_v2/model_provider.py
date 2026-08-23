"""Production editorial model provider for Director v2 (task 3, v4.4 plan).

Network-free BY DESIGN (services/** boundary): this module builds the
``build_llm_call`` seam that serializes each DirectorV2 pass request
(PROMPT_A/B/C system text + the canonical request JSON as DATA) into an
OpenAI-Responses structured-output body and hands it to an INJECTED
``HttpPost`` transport. The real transport lives in the CLI layer
(``services/cli/live_editorial_v2.py``); nothing here imports
urllib/http/socket. The pin/runtime models, loaders, typed errors, and the
``HttpPost`` Protocol live in ``editorial_pins.py`` (the low layer) and are
re-exported here so consumers need exactly one import site.

Failure discipline (PRD v4.4: never a silent heuristic fallback in product
mode): every failure is a typed :class:`EditorialRuntimeError` —
``production-model-unavailable`` / ``model-timeout`` / ``model-http-status``
/ ``model-bad-response`` / ``model-redirect-refused``. The returned callable
yields the RAW parsed JSON object; draft-model validation stays in
``director_v2`` (the existing ``LlmCallV2`` contract).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from services.editorial_v2.editorial_pins import (
    DIRECTOR_PIN_PATH,
    EDITORIAL_RUNTIME_PATH,
    MOMENT_REVIEW_PIN_PATH,
    REVIEW_INTERPRETER_PIN_PATH,
    EditorialHttpResponseError,
    EditorialPinV2,
    EditorialRedirectRefusedError,
    EditorialRuntimeError,
    EditorialRuntimeV1,
    HttpPost,
    load_editorial_pin,
    load_editorial_runtime,
)
from services.editorial_v2.prompt_v2 import (
    PROMPT_TEXTS,
    CreativeEditDraft,
    MomentSelectionDraft,
    StoryPlanDraft,
)
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.contracts.primitives import StrictModel
    from services.editorial_v2.director_v2 import LlmCallV2
    from services.editorial_v2.prompt_v2 import PassName

#: Bounded per-call budget (task 3): a hung model call fails typed at 120 s.
REQUEST_TIMEOUT_SECONDS: Final = 120.0

#: The three DirectorV2 passes map to exactly these draft models; the
#: director pin's ``structured_output_schemas`` record is cross-checked
#: against this table so the pin can never drift from the wired schemas.
_PASS_DRAFT_MODELS: Final[dict[str, type[StrictModel]]] = {
    "pass_a": StoryPlanDraft,
    "pass_b": MomentSelectionDraft,
    "pass_c": CreativeEditDraft,
}


def _request_body(
    pin: EditorialPinV2, prompt_text: str, draft_model: type[StrictModel], request: StrictModel
) -> bytes:
    """Structured-output request body (``services/cli/live_editorial.py`` pattern).

    The serialized request rides as the user-message CONTENT — input text is
    DATA for the model, never instruction text merged into the system prompt.
    """

    payload = {
        "model": pin.model_id,
        "input": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": canonical_model_bytes(request).decode("utf-8")},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": draft_model.__name__,
                "strict": True,
                "schema": draft_model.model_json_schema(),
            }
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _extract_output_text(document: object) -> str | None:
    """Pull the structured output text from an OpenAI-Responses document."""

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


def _parse_response(raw: bytes) -> dict[str, object]:
    """Transport bytes → the raw JSON object for director_v2 to validate."""

    try:
        document = json.loads(raw.decode("utf-8"))
        text = _extract_output_text(document)
        payload = None if text is None else json.loads(text)
    except (ValueError, UnicodeDecodeError) as error:
        raise EditorialRuntimeError(
            "model-bad-response", f"unparseable structured output from the model: {error}"
        ) from error
    if text is None:
        raise EditorialRuntimeError(
            "model-bad-response", "the response carries no structured output text"
        )
    if not isinstance(payload, dict):
        raise EditorialRuntimeError(
            "model-bad-response",
            f"structured output is not a JSON object (got {type(payload).__name__})",
        )
    return payload


def _check_schema_record(pin: EditorialPinV2) -> None:
    """Refuse a director pin whose recorded schemas drifted from the wiring."""

    for pass_name, recorded in (pin.structured_output_schemas or {}).items():
        wired = _PASS_DRAFT_MODELS.get(pass_name)
        if wired is not None and recorded != wired.__name__:
            raise EditorialRuntimeError(
                "production-model-unavailable",
                f"pin {pin.purpose} records {pass_name}->{recorded} but the seam wires "
                f"{pass_name}->{wired.__name__}; refusing a drifted pin",
            )


def build_llm_call(pin: EditorialPinV2, http_post: HttpPost | None = None) -> LlmCallV2:
    """Build the DirectorV2 ``llm_call`` seam over an injected transport.

    ``http_post=None`` means no transport could be constructed (env gate
    unset) — in production mode that is a typed
    ``production-model-unavailable`` refusal, NEVER a heuristic fallback.
    """

    if http_post is None:
        raise EditorialRuntimeError(
            "production-model-unavailable",
            "production editorial mode requires an injected HTTP transport; none was "
            "provided (env gate unset or transport construction refused) — refusing "
            "before any model call, never falling back to the heuristic planner",
        )
    _check_schema_record(pin)

    def call(pass_name: PassName, request: StrictModel) -> object:
        draft_model = _PASS_DRAFT_MODELS[pass_name]
        body = _request_body(pin, PROMPT_TEXTS[pass_name], draft_model, request)
        try:
            raw = http_post(
                pin.endpoint,
                {"Content-Type": "application/json"},
                body,
                timeout_s=REQUEST_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            raise EditorialRuntimeError(
                "model-timeout",
                f"the pinned editorial model call exceeded {REQUEST_TIMEOUT_SECONDS:.0f}s",
            ) from error
        except EditorialHttpResponseError as error:
            raise EditorialRuntimeError(
                "model-http-status",
                f"the pinned editorial endpoint answered HTTP {error.status_code}: "
                f"{error.detail}",
            ) from error
        except EditorialRedirectRefusedError as error:
            raise EditorialRuntimeError(
                "model-redirect-refused",
                f"the pinned editorial endpoint redirected to {error.redirect_to!r}; "
                "redirects are refused so the bearer credential can never be replayed",
            ) from error
        return _parse_response(raw)

    return call


__all__ = [
    "DIRECTOR_PIN_PATH",
    "EDITORIAL_RUNTIME_PATH",
    "MOMENT_REVIEW_PIN_PATH",
    "REQUEST_TIMEOUT_SECONDS",
    "REVIEW_INTERPRETER_PIN_PATH",
    "EditorialHttpResponseError",
    "EditorialPinV2",
    "EditorialRedirectRefusedError",
    "EditorialRuntimeError",
    "EditorialRuntimeV1",
    "HttpPost",
    "build_llm_call",
    "load_editorial_pin",
    "load_editorial_runtime",
]
