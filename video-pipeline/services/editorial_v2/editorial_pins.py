"""Editorial v2 pins, runtime config, and the transport contract (task 3).

The low layer of the production editorial model path (network-free BY
DESIGN, services/** boundary): the typed failure vocabulary shared by the
seam and the CLI transport, the ``HttpPost`` transport Protocol that real
transports (CLI layer only) implement, the three INDEPENDENT logical model
pins (``editorial-pin-v2``), and the runtime config model
(``config/editorial-runtime.json``, ``editorial-runtime-v1``) with its
strict canonical loaders. ``services/editorial_v2/model_provider.py`` builds
the DirectorV2 ``llm_call`` seam on top of this layer and re-exports this
module's public surface.

Credential policy: pins record env-var NAMES ONLY; values never enter
messages, records, or error details.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from services.contracts.primitives import StrictModel
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

EDITORIAL_RUNTIME_PATH: Final = Path("config/editorial-runtime.json")
DIRECTOR_PIN_PATH: Final = Path("config/toolchains/pins/editorial-director-v2.json")
MOMENT_REVIEW_PIN_PATH: Final = Path("config/toolchains/pins/moment-review-multimodal.json")
REVIEW_INTERPRETER_PIN_PATH: Final = Path("config/toolchains/pins/review-interpreter.json")

_ENV_NAME_PATTERN = "must be an env-var NAME (UPPER_SNAKE), never a value"
_HTTPS_PREFIX = "https://"


class EditorialRuntimeError(Exception):
    """Typed production-model failure — never a silent heuristic fallback."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class EditorialHttpResponseError(Exception):
    """Transport-raised: the pinned endpoint answered a non-2xx status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class EditorialRedirectRefusedError(Exception):
    """Transport-raised: the pinned endpoint answered a redirect (refused)."""

    def __init__(self, redirect_to: str) -> None:
        super().__init__(f"redirect refused to {redirect_to!r}")
        self.redirect_to = redirect_to


class HttpPost(Protocol):
    """One bounded HTTPS POST; real transports live in the CLI layer only."""

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float
    ) -> bytes: ...


class CodexRunner(Protocol):
    """One bounded ``codex exec`` call → the assistant's FINAL message text.

    The Codex-subscription transport of the editorial pins (owner decision,
    v44-first-publish-delta): the real implementation (binary gate, login
    probe, read-only sandbox subprocess, ``--output-last-message`` capture)
    lives in the CLI layer only (``services/cli/live_editorial_codex.py``);
    services code receives it injected, exactly like :class:`HttpPost`.
    """

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str: ...


class EditorialPinV2(StrictModel):
    """One logical production-model pin (three independent pins; no framework).

    Same model family today, but each editorial purpose carries its OWN pin
    so any one purpose can be re-pinned without touching the others
    (user decision D2). ``external_credentials`` lists env-var NAMES ONLY —
    and is meaningful ONLY on the ``openai-responses-structured-output``
    surface: a ``codex-exec`` pin authenticates via ``codex login`` (CLI
    state, no env credential), so it carries neither endpoint nor env names.
    """

    schema_version: Literal["editorial-pin-v2"]
    purpose: str = Field(min_length=1, strict=True)
    model_id: str = Field(min_length=1, strict=True)
    api_surface: Literal["openai-responses-structured-output", "codex-exec"]
    endpoint: str | None = Field(default=None, min_length=1, strict=True)
    external_credentials: tuple[str, ...] = Field(default=())
    network_env: str | None = Field(default=None, min_length=1, strict=True)
    structured_output_schemas: dict[str, str] | None = None

    @field_validator("endpoint")
    @classmethod
    def https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(_HTTPS_PREFIX):
            raise ValueError(f"endpoint {value!r} must be an https:// URL (pinned surface)")
        return value

    @field_validator("external_credentials", "network_env")
    @classmethod
    def names_not_values(
        cls, value: str | tuple[str, ...] | None
    ) -> str | tuple[str, ...] | None:
        names = value if isinstance(value, tuple) else (value,)
        for name in names:
            if name is not None and (not name.isupper() or " " in name or "=" in name):
                raise ValueError(f"credential env entry {name!r} {_ENV_NAME_PATTERN}")
        return value

    @model_validator(mode="after")
    def surface_fields_are_consistent(self) -> Self:
        """Endpoint/env-credential fields exist ONLY where the surface uses them.

        A half-configured pin (codex pin still naming an endpoint, or an
        openai pin missing its endpoint/env gate names) is a DRIFTED pin —
        refuse it rather than guess which field wins.
        """

        if self.api_surface == "openai-responses-structured-output":
            if self.endpoint is None or not self.external_credentials or self.network_env is None:
                raise ValueError(
                    "api_surface openai-responses-structured-output requires endpoint, "
                    "external_credentials, and network_env (env-var names only)"
                )
        elif self.endpoint is not None or self.external_credentials or self.network_env is not None:
            raise ValueError(
                "api_surface codex-exec authenticates via `codex login`; endpoint, "
                "external_credentials, and network_env must be absent"
            )
        return self


class EditorialRuntimeV1(StrictModel):
    """Which editorial brain a run uses, and where its pins live.

    ``production_model`` (the shipped default) wires DirectorV2's three
    passes to the pinned model and BLOCKS when the provider is unavailable;
    ``heuristic_diagnostic`` is the explicit no-LLM diagnostic path. Paths
    are relative to the video-pipeline root.

    ``transport`` selects HOW the pinned model is reached (owner decision,
    v44-first-publish-delta): ``codex-exec`` (DEFAULT — the Codex
    subscription via ``codex exec``; gates on the codex CLI being installed
    and logged in) or ``openai-api`` (the env-gated OpenAI API key path).
    """

    schema_version: Literal["editorial-runtime-v1"]
    mode: Literal["production_model", "heuristic_diagnostic"]
    transport: Literal["codex-exec", "openai-api"] = "codex-exec"
    director_pin_path: str = Field(min_length=1, strict=True)
    moment_review_pin_path: str = Field(min_length=1, strict=True)
    review_interpreter_pin_path: str = Field(min_length=1, strict=True)

    @field_validator("director_pin_path", "moment_review_pin_path", "review_interpreter_pin_path")
    @classmethod
    def relative_paths_only(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"pin path {value!r} must be relative to the video-pipeline root")
        return value


def _load_canonical[ModelT: StrictModel](path: Path, model: type[ModelT], what: str) -> ModelT:
    try:
        raw = path.read_bytes()
        parsed = model.model_validate_json(raw)
    except OSError as error:
        raise EditorialRuntimeError(
            "production-model-unavailable", f"cannot read {what} at {path}: {error}"
        ) from error
    except ValueError as error:
        raise EditorialRuntimeError(
            "production-model-unavailable", f"{what} at {path} is malformed: {error}"
        ) from error
    if raw != canonical_model_bytes(parsed):
        raise EditorialRuntimeError(
            "production-model-unavailable", f"{what} at {path} is not canonical JSON"
        )
    return parsed


def load_editorial_pin(path: Path = DIRECTOR_PIN_PATH) -> EditorialPinV2:
    """Load and strictly validate one editorial pin (canonical bytes only)."""

    return _load_canonical(path, EditorialPinV2, "editorial pin")


def load_editorial_runtime(path: Path = EDITORIAL_RUNTIME_PATH) -> EditorialRuntimeV1:
    """Load the editorial runtime config and verify its pin files exist."""

    runtime = _load_canonical(path, EditorialRuntimeV1, "editorial runtime config")
    for field in ("director_pin_path", "moment_review_pin_path", "review_interpreter_pin_path"):
        pin_path = Path(getattr(runtime, field))
        if not pin_path.is_file():
            raise EditorialRuntimeError(
                "production-model-unavailable",
                f"editorial runtime {path} names a missing pin file: {pin_path}",
            )
    return runtime


__all__ = [
    "DIRECTOR_PIN_PATH",
    "EDITORIAL_RUNTIME_PATH",
    "MOMENT_REVIEW_PIN_PATH",
    "REVIEW_INTERPRETER_PIN_PATH",
    "CodexRunner",
    "EditorialHttpResponseError",
    "EditorialPinV2",
    "EditorialRedirectRefusedError",
    "EditorialRuntimeError",
    "EditorialRuntimeV1",
    "HttpPost",
    "load_editorial_pin",
    "load_editorial_runtime",
]
