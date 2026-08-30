"""Provider-compatible response-schema projection for the Gemini wire.

MEASURED 2026-08-30 (T11 real Arm B run): ``generateContent`` answers
HTTP 400 INVALID_ARGUMENT for pydantic's raw ``model_json_schema`` output —
the endpoint named, in two measured rounds: ``additionalProperties``
(StrictModel emits ``additionalProperties: false``) and ``$defs``/``$ref``
(nested DTO models). The wire schema is therefore PROJECTED: nested-model
references are INLINED and unsupported keywords dropped before the schema
is embedded in a request body. Local parsing keeps using the strict models,
so provider-output strictness is unchanged — only the transmitted schema
shape is adapted to the provider's accepted subset.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

#: JSON-Schema keywords the Gemini REST response_schema parser rejects
#: (measured; the endpoint's own fieldViolations named each one).
_UNSUPPORTED_SCHEMA_KEYS: Final = frozenset({"additionalProperties", "$defs"})

#: Inline depth bound: our DTOs are non-recursive, so real resolution needs
#: < 5 levels; anything deeper is a cycle and fails typed, not by recursion.
_MAX_REF_DEPTH: Final = 8

_REF_SENTINEL: Final = "$ref"


class WireSchemaError(ValueError):
    """Typed projection refusal (unresolvable or cyclic schema reference)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _inline(node: object, defs: dict[str, object], depth: int) -> object:
    if depth > _MAX_REF_DEPTH:
        raise WireSchemaError(
            "schema-ref-depth-exceeded",
            f"reference inlining exceeded depth {_MAX_REF_DEPTH} (cyclic schema?)",
        )
    if isinstance(node, dict):
        ref = node.get(_REF_SENTINEL)
        if isinstance(ref, str):
            target = ref.removeprefix("#/$defs/")
            definition = defs.get(target)
            if not isinstance(definition, dict):
                raise WireSchemaError(
                    "schema-ref-unresolvable", f"no $defs entry for {target!r}"
                )
            return _inline(definition, defs, depth + 1)
        return {
            key: _inline(value, defs, depth)
            for key, value in node.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(node, list):
        return [_inline(item, defs, depth) for item in node]
    return node


def gemini_wire_schema(model: type[BaseModel]) -> dict[str, object]:
    """The model's JSON schema with nested models inlined and unsupported
    keywords removed (the provider's accepted subset)."""

    raw: object = model.model_json_schema()
    if not isinstance(raw, dict):
        raise TypeError("model_json_schema must be a JSON object")
    defs_raw = raw.get("$defs", {})
    if not isinstance(defs_raw, dict):
        raise WireSchemaError("schema-defs-invalid", "$defs is not an object")
    projected = _inline(raw, defs_raw, 0)
    if not isinstance(projected, dict):
        raise TypeError("model_json_schema must project to a JSON object")
    return projected


__all__ = ["WireSchemaError", "gemini_wire_schema"]
