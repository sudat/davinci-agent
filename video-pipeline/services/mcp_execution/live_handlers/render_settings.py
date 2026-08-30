"""Format/codec and per-setting apply for the native render handler."""

from __future__ import annotations

from collections.abc import Mapping

from services.mcp_client.ops_models import (
    FormatCodecResult,
    RenderCodecsResult,
    RenderFormatsResult,
    SafeSetRenderResult,
    ValidatedSettings,
)
from services.mcp_execution.live_handlers.common import LiveAdapterError, LiveSessionContext


def verify_format_codec(ctx: LiveSessionContext, format_id: str, codec_id: str) -> None:
    formats = RenderFormatsResult.model_validate(ctx.transport("render", "get_formats", {}))
    if formats.formats is None or not isinstance(formats.formats, dict):
        raise LiveAdapterError("render-formats-unreadable", "get_render_formats returned no dict")
    names = set(formats.formats) | set(formats.formats.values())
    if format_id not in names:
        keys = list(formats.formats.keys())
        raise LiveAdapterError(
            "render-format-not-found", f"format {format_id!r} not in {keys}"
        )
    codecs = RenderCodecsResult.model_validate(
        ctx.transport("render", "get_codecs", {"format": format_id})
    )
    if codecs.codecs is None or not isinstance(codecs.codecs, dict):
        raise LiveAdapterError("render-codecs-unreadable", "get_render_codecs returned no dict")
    labels = {k for k, v in codecs.codecs.items()} | set(codecs.codecs.values())
    if codec_id not in labels:
        raise LiveAdapterError(
            "render-codec-not-found", f"codec {codec_id!r} not for format {format_id!r}"
        )
    pin = FormatCodecResult.model_validate(
        ctx.transport(
            "render",
            "set_format_and_codec",
            {"format": format_id, "codec": codec_id},
        )
    )
    if not pin.ok:
        raise LiveAdapterError(
            "render-format-codec-not-applied",
            f"set_format_and_codec refused: {pin.error}",
        )
    current = FormatCodecResult.model_validate(
        ctx.transport("render", "get_format_and_codec", {})
    )
    got_format = current.format or current.format_id
    got_codec = current.codec or current.codec_id
    if got_format is None or got_codec is None:
        raise LiveAdapterError(
            "render-format-codec-readback-missing",
            f"get_format_and_codec returned incomplete identifiers: "
            f"format={got_format!r} codec={got_codec!r}",
        )
    if got_format != format_id or got_codec != codec_id:
        raise LiveAdapterError(
            "render-format-codec-mismatch",
            f"requested {format_id}/{codec_id} but readback is {got_format}/{got_codec}",
        )


def per_setting_apply(
    ctx: LiveSessionContext, settings: Mapping[str, object]
) -> list[dict[str, object]]:
    applied: list[dict[str, object]] = []
    for key, value in settings.items():
        single: dict[str, object] = {key: value}
        validated = ValidatedSettings.model_validate(
            ctx.transport("render", "validate_render_settings", {"settings": single})
        )
        if validated.valid is not True:
            raise LiveAdapterError(
                "render-setting-not-validated",
                f"{key}={value!r} not valid: {validated.errors}",
            )
        if validated.errors:
            raise LiveAdapterError(
                "render-setting-validation-error",
                f"{key} errors: {validated.errors}",
            )
        result = SafeSetRenderResult.model_validate(
            ctx.transport("render", "safe_set_render_settings", {"settings": single})
        )
        if not result.ok:
            raise LiveAdapterError(
                "render-setting-not-applied", f"{key} set refused: {result.error}"
            )
        applied.append(
            {
                "key": key,
                "value": value,
                "validation": validated.model_dump(mode="json"),
                "set": result.model_dump(mode="json"),
            }
        )
    return applied


__all__ = ["per_setting_apply", "verify_format_codec"]
