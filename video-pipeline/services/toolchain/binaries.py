from __future__ import annotations

from pathlib import Path

from pydantic import field_validator

from services.contracts.primitives import Sha256, StrictModel


class BinaryRecord(StrictModel):
    path: str
    sha256: Sha256
    version_output: str

    @field_validator("path")
    @classmethod
    def require_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("binary path must be absolute")
        return value
