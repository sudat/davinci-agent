"""Typed scope guard for presentation payloads (Todo 49).

The Phase-2 fixed presentation section accepts exactly the frozen surface —
anything Phase-3 (style profiles, BGM/SE entries, Fusion/Fairlight
processing, camera presets, channel branches) is refused with the typed
``phase3-scope-rejected`` error before validation even runs, and malformed
sections fail closed as ``presentation-invalid``. Intro/outro assets without
{path, sha256, license_ref} provenance surface as ``unapproved-asset``.
"""

from __future__ import annotations

import json
from typing import Final

from pydantic import ValidationError

from services.resolve_adapter.errors import (
    AUDIO_ROLE_CONFLATION,
    PHASE3_SCOPE,
    PRESENTATION_INVALID,
    UNAPPROVED_ASSET,
    PackageCompileError,
)
from services.resolve_adapter.presentation_models import PresentationSection

PHASE3_KEYS: Final[frozenset[str]] = frozenset(
    {
        "style_profile",
        "style_profiles",
        "bgm",
        "bgm_se",
        "se",
        "sound_effect",
        "sound_effects",
        "fusion",
        "fusion_template",
        "fairlight",
        "fairlight_preset",
        "camera_preset",
        "camera",
        "channel_profile",
        "channel_branch",
        "ducking_profile",
        "loudness_target",
        "eq_profile",
        "gain_db",
    }
)

PROVENANCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"path", "sha256", "license_ref", "asset"}
)


def _scan_phase3_keys(node: object, where: str) -> None:
    if isinstance(node, dict):
        for key, child in node.items():
            if isinstance(key, str) and key in PHASE3_KEYS:
                raise PackageCompileError(
                    PHASE3_SCOPE, f"{where}{key} is Phase-3 config, refused"
                )
            _scan_phase3_keys(child, where)
    elif isinstance(node, list | tuple):
        for child in node:
            _scan_phase3_keys(child, where)


def _as_object(payload: object) -> dict[str, object]:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode()
        except UnicodeDecodeError as error:
            raise PackageCompileError(
                PRESENTATION_INVALID, f"presentation bytes are not UTF-8: {error}"
            ) from error
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as error:
            raise PackageCompileError(
                PRESENTATION_INVALID, f"presentation is not valid JSON: {error}"
            ) from error
    if not isinstance(payload, dict):
        raise PackageCompileError(
            PRESENTATION_INVALID,
            f"presentation section must be a JSON object: {type(payload)!r}",
        )
    return payload


def _typed_validation_failure(error: ValidationError) -> PackageCompileError:
    for issue in error.errors():
        if issue["type"] == "role_conflation":
            return PackageCompileError(AUDIO_ROLE_CONFLATION, str(error))
        loc = issue["loc"]
        if (
            loc
            and loc[0] == "intro_outro"
            and str(loc[-1]) in PROVENANCE_FIELDS
        ):
            return PackageCompileError(
                UNAPPROVED_ASSET,
                f"intro/outro asset without provenance: {issue['msg']}",
            )
    return PackageCompileError(PRESENTATION_INVALID, str(error))


def parse_presentation_section(payload: object) -> PresentationSection:
    """Parse a presentation payload; Phase-3 keys and malformed input fail typed."""

    document = _as_object(payload)
    _scan_phase3_keys(document, "presentation.")
    try:
        # JSON semantics: deserialized artifacts arrive with lists, not tuples.
        return PresentationSection.model_validate_json(json.dumps(document))
    except ValidationError as error:
        raise _typed_validation_failure(error) from error


__all__ = ["PHASE3_KEYS", "parse_presentation_section"]
