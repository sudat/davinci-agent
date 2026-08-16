from __future__ import annotations

import json
from typing import Never

from pydantic import BaseModel


class CanonicalJsonFloatError(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return f"floats are forbidden in canonical JSON: {self.value}"


def _reject_float(value: str) -> Never:
    raise CanonicalJsonFloatError(value)


def canonical_json_bytes(model: BaseModel) -> bytes:
    serialized = json.dumps(
        model.model_dump(mode="json", by_alias=True, exclude_none=False),
        allow_nan=False,
        ensure_ascii=False,
    )
    value = json.loads(
        serialized,
        parse_constant=_reject_float,
        parse_float=_reject_float,
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
