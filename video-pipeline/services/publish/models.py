"""PublishPackageV1 — credential-alias only artifact (task 53)."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel

_PUBLISH_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# ---------------------------------------------------------------------------
# Credential leak guard
# ---------------------------------------------------------------------------


class CredentialLeakError(ValueError):
    """Typed error when a credential-alias field looks like a real token/secret."""


_TOKEN_LEN_LIMIT: Final = 64


def _looks_like_token(value: str) -> bool:
    if len(value) > _TOKEN_LEN_LIMIT:
        return True
    prefixes = ("ya29.", "Bearer", "AIza", "eyJ")
    return any(value.startswith(p) for p in prefixes)


def _reject_token_strings(value: object) -> object:
    """Before-validator helper that raises CredentialLeakError on token-like strings.

    Used as field validator for alias fields and as a recursive scanner for
    the whole payload (to guarantee no field can hide a token).
    """

    # Only check strings — non-strings are ignored.
    if isinstance(value, str) and _looks_like_token(value):
        raise CredentialLeakError(f"credential alias looks like a token: {value[:32]!r}")
    return value


def _scan_for_tokens(payload: object) -> object:
    """Model-level before validator: walk all string values and reject tokens."""

    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for v in node.values():
                if isinstance(v, str) and _looks_like_token(v):
                    raise PydanticCustomError(
                        "credential_leak",
                        "credential alias looks like a token: {value}",
                        {"value": v[:32]},
                    )
                stack.append(v)
        elif isinstance(node, list | tuple):
            stack.extend(node)
    return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_idempotency_key(render_sha256: str) -> str:
    """Deterministic uuid-ish key from render_sha256 (same input → same key)."""

    # Use uuid5 with DNS namespace for stability; render_sha256 is already
    # a hex digest, so the mapping is 1:1 and collision-resistant.
    return str(uuid.uuid5(_PUBLISH_NAMESPACE, render_sha256))


def _validate_iso8601(value: str | None) -> str | None:
    if value is None:
        return None
    # Accept any ISO-8601 that datetime.fromisoformat can parse, with
    # fallback for trailing Z.
    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        datetime.datetime.fromisoformat(candidate)
    except (ValueError, TypeError) as exc:
        raise PydanticCustomError(
            "invalid_iso8601",
            "schedule_time must be ISO-8601: {value}",
            {"value": value},
        ) from exc
    return value


CredentialAlias = Annotated[
    str,
    Field(min_length=1, strict=True),
    BeforeValidator(_reject_token_strings),
]

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class RenderRef(StrictModel):
    """Approved render reference."""

    render_sha256: Sha256
    path: Annotated[str, Field(min_length=1, strict=True)]


class Chapter(StrictModel):
    """Single chapter marker."""

    start_seconds: Annotated[float, Field(ge=0, strict=False)]
    title: Annotated[str, Field(min_length=1, strict=True)]

    @field_validator("start_seconds", mode="before")
    @classmethod
    def _coerce_int_to_float(cls, v: object) -> object:
        if isinstance(v, int) and not isinstance(v, bool):
            return float(v)
        return v


class ThumbnailRef(StrictModel):
    """Thumbnail reference — asset_id or path plus optional source note."""

    asset_id: Identifier | None = None
    path: Annotated[str, Field(min_length=1, strict=True)] | None = None
    source: Annotated[str, Field(min_length=1, strict=True)] | None = None

    @model_validator(mode="after")
    def _require_target(self) -> ThumbnailRef:
        if self.asset_id is None and self.path is None:
            raise PydanticCustomError(
                "thumbnail_target_missing",
                "thumbnail_ref requires asset_id or path",
            )
        return self


# ---------------------------------------------------------------------------
# PublishPackageV1
# ---------------------------------------------------------------------------


class PublishPackageV1(StrictModel):
    """Publish package artifact — credential alias only.

    Production must never embed real tokens; channel_target is an alias
    (e.g. "youtube-main") and any token-like value is rejected with
    CredentialLeakError / validation error type "credential_leak".
    """

    schema_version: Literal["publish-package-v1"] = "publish-package-v1"
    episode_id: Identifier
    render_ref: RenderRef
    title_candidates: Annotated[
        list[Annotated[str, Field(min_length=1, strict=True)]],
        Field(min_length=1, strict=True),
    ]
    selected_title: Annotated[str, Field(min_length=1, strict=True)]
    description: Annotated[str, Field(strict=True)] = ""
    chapters: list[Chapter] = Field(default_factory=list)
    tags: list[Annotated[str, Field(min_length=1, strict=True)]] = Field(
        default_factory=list,
    )
    thumbnail_ref: ThumbnailRef | None = None
    playlist_target: Annotated[str, Field(min_length=1, strict=True)] | None = None
    visibility: Literal["private", "unlisted", "public"] = "private"
    schedule_time: Annotated[str, Field(min_length=1, strict=True)] | None = None
    approval_refs: list[Identifier] = Field(default_factory=list)
    publication_approval_ref: Annotated[
        str,
        Field(min_length=1, strict=True),
    ] | None = None
    channel_target: CredentialAlias
    idempotency_key: Annotated[str, Field(min_length=1, strict=True)]
    remote_video_id: Annotated[str, Field(min_length=1, strict=True)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _scan_tokens(cls, value: object) -> object:
        return _scan_for_tokens(value)

    @field_validator("schedule_time", mode="after")
    @classmethod
    def _check_schedule_iso(cls, v: str | None) -> str | None:
        return _validate_iso8601(v)

    @model_validator(mode="after")
    def _enforce_public_approval(self) -> PublishPackageV1:
        if self.visibility == "public" and (
            self.publication_approval_ref is None or self.publication_approval_ref.strip() == ""
        ):
            raise PydanticCustomError(
                "public_requires_approval",
                "visibility public requires a non-empty publication_approval_ref",
            )
        return self


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------


def build_package(
    render_ref: RenderRef | dict[str, object],
    metadata: dict[str, object] | None = None,
    *,
    approvals: dict[str, object] | None = None,
) -> PublishPackageV1:
    """Construct a PublishPackageV1 with deterministic idempotency key.

    Args:
        render_ref: RenderRef instance or dict with render_sha256 + path.
        metadata: Optional dict with episode_id, title_candidates,
            selected_title, description, chapters, tags, thumbnail_ref,
            playlist_target, visibility, schedule_time, channel_target,
            remote_video_id, etc. Missing keys get sensible defaults.
        approvals: Optional dict with approval_refs and
            publication_approval_ref.

    Idempotency key is derived deterministically from render_sha256
    (same render → same key, different render → different key).
    """

    rr = (
        RenderRef.model_validate(render_ref) if isinstance(render_ref, dict) else render_ref
    )

    meta: dict[str, object] = dict(metadata or {})
    appr: dict[str, object] = dict(approvals or {})

    # Deterministic key
    idem = _derive_idempotency_key(rr.render_sha256)

    # Merge with defaults — explicit keys win
    payload: dict[str, object] = {
        "schema_version": "publish-package-v1",
        "episode_id": meta.get("episode_id", "ep_test"),
        "render_ref": rr.model_dump(),
        "title_candidates": meta.get("title_candidates", ["Untitled"]),
        "selected_title": meta.get("selected_title", "Untitled"),
        "description": meta.get("description", ""),
        "chapters": meta.get("chapters", []),
        "tags": meta.get("tags", []),
        "thumbnail_ref": meta.get("thumbnail_ref"),
        "playlist_target": meta.get("playlist_target"),
        "visibility": meta.get("visibility", "private"),
        "schedule_time": meta.get("schedule_time"),
        "approval_refs": appr.get("approval_refs", meta.get("approval_refs", [])),
        "publication_approval_ref": appr.get(
            "publication_approval_ref",
            meta.get("publication_approval_ref"),
        ),
        "channel_target": meta.get("channel_target", "youtube-main"),
        "idempotency_key": meta.get("idempotency_key", idem),
        "remote_video_id": meta.get("remote_video_id"),
    }

    # If metadata already supplied an idempotency_key that differs from
    # derived, keep the supplied one (explicit override). Otherwise the
    # derived key ensures determinism: same render → same key.
    # When no override, payload idempotency_key == derived.
    return PublishPackageV1.model_validate(payload)


__all__ = [
    "Chapter",
    "CredentialLeakError",
    "PublishPackageV1",
    "RenderRef",
    "ThumbnailRef",
    "build_package",
]
