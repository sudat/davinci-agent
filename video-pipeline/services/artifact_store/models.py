from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactId,
    Sha256,
    StrictModel,
)

ShardName = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{2}$")]


class ContentAddressedRef(StrictModel):
    store_root: str
    shard: ShardName
    sha256: Sha256
    size: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_shard_prefix(self) -> ContentAddressedRef:
        if self.shard != self.sha256[:2]:
            raise PydanticCustomError(
                "shard_prefix",
                "shard must be the first two hex characters of the sha256",
            )
        return self

    def object_relative_path(self) -> str:
        return f"objects/{self.shard}/{self.sha256}"


class PublicationIntent(StrictModel):
    record_type: Literal["publication-intent"] = "publication-intent"
    envelope: ArtifactEnvelope[str]


class PublicationReceipt(StrictModel):
    record_type: Literal["publication-receipt"] = "publication-receipt"
    artifact_id: ArtifactId
    content_sha256: Sha256
    object_ref: ContentAddressedRef
    meta_relative_path: str
    idempotent: bool


__all__ = [
    "ContentAddressedRef",
    "PublicationIntent",
    "PublicationReceipt",
]
