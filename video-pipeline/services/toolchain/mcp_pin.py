"""Strict pin contract for the vendored davinci-resolve-mcp deployment.

The pin file is the authoritative record of WHERE the external MCP server
is deployed from (one exact commit), HOW it is launched (compound stdio
server, its own venv, advanced Node sibling), and WHICH guarantees were
established at pin time (update checks disabled, Resolve preference).
Loading it is a boundary parse: anything that does not match the frozen
shape is a typed :class:`McpPinError`, never a silent default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import StringConstraints, ValidationError

from services.contracts.primitives import StrictModel

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$", strict=True)]
PythonVersion = Annotated[
    str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)
]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://\S+$", strict=True)]
RelativePosixPath = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_./-]*$", strict=True)
]
UtcIsoDate = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", strict=True)
]


class AdvancedServerPin(StrictModel):
    package: Literal["davinci-resolve-advanced-mcp"]
    enabled: bool


class OptionalDepsPin(StrictModel):
    ffmpeg: Literal["required"]


class UpdateCheckPin(StrictModel):
    """``enabled`` is ``Literal[False]``: a pin that allows update checks is invalid."""

    enabled: Literal[False]
    mechanism: str


class McpPin(StrictModel):
    schema_version: Literal["mcp-pin-v1"]
    source_url: HttpsUrl
    commit: CommitSha
    server_mode: Literal["compound"]
    server_entry_point: RelativePosixPath
    server_protocol: Literal["stdio"]
    venv_python: str
    venv_python_version: PythonVersion
    repo_python: PythonVersion
    advanced_server: AdvancedServerPin
    optional_deps: OptionalDepsPin
    resolve_preference: Literal["Local"]
    update_check: UpdateCheckPin
    pinned_date: UtcIsoDate


class McpPinError(Exception):
    """The pin file is missing, unreadable, or does not match ``mcp-pin-v1``."""


def load_mcp_pin(path: Path) -> McpPin:
    """Parse and validate the pin file at ``path`` (boundary parse)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpPinError(f"cannot read pin file {path}: {exc}") from exc
    try:
        return McpPin.model_validate(payload)
    except ValidationError as exc:
        raise McpPinError(f"pin file {path} violates mcp-pin-v1: {exc}") from exc


__all__ = ["AdvancedServerPin", "McpPin", "McpPinError", "UpdateCheckPin", "load_mcp_pin"]
