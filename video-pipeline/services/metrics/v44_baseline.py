"""v44 baseline freeze — strict model ``v44-baseline-v1``."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StringConstraints

from services.contracts.primitives import Sha256, StrictModel


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


CommitSha40 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$", strict=True)]


class PytestSummary(StrictModel):
    """Offline pytest summary embedded in the baseline record."""

    passed: Annotated[int, Field(ge=0, strict=True)]
    failed: Annotated[int, Field(ge=0, strict=True)]
    skipped: Annotated[int, Field(ge=0, strict=True)]
    tail: Annotated[str, Field(strict=True)]


class BaselineRecordV1(StrictModel):
    """Frozen v4.4 baseline evidence (``v44-baseline-v1``)."""

    schema_version: Literal["v44-baseline-v1"] = "v44-baseline-v1"
    baseline_commit_sha: CommitSha40
    current_commit_sha: CommitSha40
    pytest_summary: PytestSummary
    ruff_result: Annotated[str, Field(min_length=1, strict=True)]
    basedpyright_result: Annotated[str, Field(min_length=1, strict=True)]
    mcp_fit_sha256: Sha256
    v43_gate_evidence: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    backends: dict[str, str]
    created_at: Annotated[str, Field(min_length=1, strict=True)]
    notes: str | None = None


__all__ = ["BaselineRecordV1", "CommitSha40", "PytestSummary"]
