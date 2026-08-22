"""Operator-facing error cards (PRD 13.4): what failed, what is next.

The card surface carries structure only — failed stage, affected
output, retry safety, fallback availability, the next-action hint, and
a ``debug_ref`` pointing at the log file. Exception messages and stack
traces NEVER reach the card: they stay in the debug log the reference
points at. Retry safety is classified from the typed error code or the
exception type; anything unclassifiable stays ``None`` (unknown)
instead of a guess.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel

type NonEmpty = Annotated[str, Field(min_length=1, strict=True)]

_RETRY_SAFE_TYPES: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)
_UNSAFE_TYPES: tuple[type[BaseException], ...] = (ValueError,)

_RETRY_SAFE_MARKERS: tuple[str, ...] = ("timeout", "transient", "locked", "unreachable")
_UNSAFE_MARKERS: tuple[str, ...] = (
    "schema",
    "validation",
    "conflict",
    "missing",
    "not-found",
    "credential",
    "invalid",
    "mismatch",
)


class ErrorContext(StrictModel):
    """What the caller knows about where the failure happened."""

    failed_stage: NonEmpty | None = None
    affected_output: NonEmpty | None = None
    fallback_available: bool = False
    debug_log_path: NonEmpty


class ErrorCardV1(StrictModel):
    """The structured failure surface shown to the operator."""

    schema_version: Literal["cockpit-error-card-v1"] = "cockpit-error-card-v1"
    code: NonEmpty
    failed_stage: NonEmpty | None
    affected_output: NonEmpty | None
    retry_safe: bool | None
    fallback_available: bool
    next_action_hint: NonEmpty
    debug_ref: NonEmpty


def _error_code(source: BaseException | str) -> str:
    if isinstance(source, str):
        return source
    code = getattr(source, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(source).__name__


def _classify_retry_safe(source: BaseException | str) -> bool | None:
    if isinstance(source, str):
        lowered = source.lower()
        if any(marker in lowered for marker in _RETRY_SAFE_MARKERS):
            return True
        if any(marker in lowered for marker in _UNSAFE_MARKERS):
            return False
        return None
    if isinstance(source, _RETRY_SAFE_TYPES):
        return True
    if isinstance(source, _UNSAFE_TYPES):
        return False
    code = _error_code(source)
    return _classify_retry_safe(code)


def _next_action_hint(*, retry_safe: bool | None, fallback_available: bool) -> str:
    hint = {
        True: "この失敗は再試行できます。同じ操作を再実行してください。",
        False: "この失敗は再試行では解決しません。表示された項目を修正してから再実行してください。",
        None: "この失敗の分類は確定できません。debug_ref のログを確認してください。",
    }[retry_safe]
    if fallback_available:
        hint += " 代替経路での継続も可能です。"
    return hint


def build_error_card(source: BaseException | str, context: ErrorContext) -> ErrorCardV1:
    """Assemble the operator card for one failure; stack traces stay in the log."""

    retry_safe = _classify_retry_safe(source)
    return ErrorCardV1(
        code=_error_code(source),
        failed_stage=context.failed_stage,
        affected_output=context.affected_output,
        retry_safe=retry_safe,
        fallback_available=context.fallback_available,
        next_action_hint=_next_action_hint(
            retry_safe=retry_safe, fallback_available=context.fallback_available
        ),
        debug_ref=context.debug_log_path,
    )


__all__ = [
    "ErrorCardV1",
    "ErrorContext",
    "build_error_card",
]
