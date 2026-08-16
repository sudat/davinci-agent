from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Never

if TYPE_CHECKING:
    from pydantic import BaseModel

GENESIS_SHA256 = "0" * 64


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


def artifact_content_hash(model: BaseModel) -> str:
    """sha256 over canonical bytes with ``content_hash`` set to the genesis value.

    The self-referential content hash cannot cover itself, so the convention
    (mirrored by the evidence ledger) zeroes the field before hashing;
    verifiers recompute it exactly this way. ``model`` must be an
    ``ArtifactEnvelope`` subclass whose ``content_hash`` field is a sha256.
    """

    payload = model.model_copy(update={"content_hash": GENESIS_SHA256})
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
