"""MCP Capability Fit matrix offline validator (task 4).

Validates ``capabilities/v4.4/mcp-fit.json`` without live probing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

EXPECTED_SCHEMA_VERSION: Final = "mcp-fit-v1"
EXPECTED_PROVIDER: Final = "davinci-resolve-mcp"
EXPECTED_PROVIDER_VERSION: Final = "2.207.0"
EXPECTED_RESOLVE_BUILD: Final = "21.0.4.5"
EXPECTED_ROW_COUNT: Final = 22

VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"accepted", "failed", "partial", "not_available"}
)
VALID_FALLBACKS: Final[frozenset[str]] = frozenset(
    {"legacy_direct", "template_external", "manual"}
)
REQUIRED_HEADER_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "provider", "provider_version", "resolve_build", "capabilities"}
)
REQUIRED_ROW_KEYS: Final[frozenset[str]] = frozenset(
    {
        "capability",
        "provider",
        "provider_version",
        "resolve_build",
        "fixture",
        "status",
        "readback",
        "fallback",
        "evidence_refs",
    }
)


class McpFitError(Exception):
    """Base for all MCP fit validation errors."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class McpFitMissingKeyError(McpFitError):
    """Required key is missing."""


class McpFitInvalidStatusError(McpFitError):
    """Status enum value is invalid."""


class McpFitInvalidFallbackError(McpFitError):
    """Fallback enum value is invalid."""


class McpFitDuplicateFixtureError(McpFitError):
    """Fixture ID appears more than once."""


class McpFitDuplicateCapabilityError(McpFitError):
    """Capability appears more than once."""


class McpFitRowCountError(McpFitError):
    """Row count is not exactly 22."""


class McpFitProviderVersionMismatchError(McpFitError):
    """Row provider_version does not match header."""


class McpFitResolveBuildMismatchError(McpFitError):
    """Row resolve_build does not match header."""


class McpFitSchemaVersionError(McpFitError):
    """Header schema_version is invalid."""


def _require_dict(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise McpFitMissingKeyError("root must be an object")
    return data  # type: ignore[return-value]


def validate_mcp_fit_data(data: object) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    """Validate parsed JSON data. Returns the same dict on success.

    Raises typed :class:`McpFitError` subclasses on failure.
    """
    root = _require_dict(data)

    for key in REQUIRED_HEADER_KEYS:
        if key not in root:
            raise McpFitMissingKeyError(f"missing required header key: {key}")

    schema_version = root["schema_version"]
    if schema_version != EXPECTED_SCHEMA_VERSION:
        raise McpFitSchemaVersionError(
            f"schema_version must be {EXPECTED_SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    provider = root["provider"]
    if provider != EXPECTED_PROVIDER:
        raise McpFitMissingKeyError(f"provider must be {EXPECTED_PROVIDER!r}, got {provider!r}")

    header_provider_version = root["provider_version"]
    if header_provider_version != EXPECTED_PROVIDER_VERSION:
        raise McpFitProviderVersionMismatchError(
            f"header provider_version must be {EXPECTED_PROVIDER_VERSION!r}, "
            f"got {header_provider_version!r}"
        )

    header_resolve_build = root["resolve_build"]
    if header_resolve_build != EXPECTED_RESOLVE_BUILD:
        raise McpFitResolveBuildMismatchError(
            f"header resolve_build must be {EXPECTED_RESOLVE_BUILD!r}, "
            f"got {header_resolve_build!r}"
        )

    capabilities = root["capabilities"]
    if not isinstance(capabilities, list):
        raise McpFitMissingKeyError("capabilities must be a list")
    if len(capabilities) != EXPECTED_ROW_COUNT:
        raise McpFitRowCountError(
            f"capabilities row count must be {EXPECTED_ROW_COUNT}, got {len(capabilities)}"
        )

    seen_fixtures: set[str] = set()
    seen_capabilities: set[str] = set()

    for index, raw_row in enumerate(capabilities):
        if not isinstance(raw_row, dict):
            raise McpFitMissingKeyError(f"row {index} must be an object")
        row: dict[str, Any] = raw_row  # type: ignore[assignment]

        for key in REQUIRED_ROW_KEYS:
            if key not in row:
                raise McpFitMissingKeyError(
                    f"row {index} ({row.get('capability', '?')}) missing required key: {key}"
                )

        capability = row["capability"]
        if not isinstance(capability, str) or not capability:
            raise McpFitMissingKeyError(f"row {index} capability must be a non-empty string")
        if capability in seen_capabilities:
            raise McpFitDuplicateCapabilityError(
                f"duplicate capability: {capability!r} at row {index}"
            )
        seen_capabilities.add(capability)

        fixture = row["fixture"]
        if not isinstance(fixture, str) or not fixture:
            raise McpFitMissingKeyError(f"row {index} fixture must be a non-empty string")
        if fixture in seen_fixtures:
            raise McpFitDuplicateFixtureError(
                f"duplicate fixture: {fixture!r} at row {index}"
            )
        seen_fixtures.add(fixture)

        status = row["status"]
        if status not in VALID_STATUSES:
            raise McpFitInvalidStatusError(
                f"row {index} ({capability}) invalid status: {status!r} "
                f"expected one of {sorted(VALID_STATUSES)}"
            )

        fallback = row["fallback"]
        if fallback not in VALID_FALLBACKS:
            raise McpFitInvalidFallbackError(
                f"row {index} ({capability}) invalid fallback: {fallback!r} "
                f"expected one of {sorted(VALID_FALLBACKS)}"
            )

        if row["provider"] != EXPECTED_PROVIDER:
            raise McpFitMissingKeyError(
                f"row {index} ({capability}) provider must be {EXPECTED_PROVIDER!r}"
            )
        if row["provider_version"] != header_provider_version:
            raise McpFitProviderVersionMismatchError(
                f"row {index} ({capability}) provider_version "
                f"{row['provider_version']!r} != header {header_provider_version!r}"
            )
        if row["resolve_build"] != header_resolve_build:
            raise McpFitResolveBuildMismatchError(
                f"row {index} ({capability}) resolve_build "
                f"{row['resolve_build']!r} != header {header_resolve_build!r}"
            )

        evidence_refs = row["evidence_refs"]
        if not isinstance(evidence_refs, list):
            raise McpFitMissingKeyError(f"row {index} ({capability}) evidence_refs must be a list")

        readback = row["readback"]
        if not isinstance(readback, str):
            raise McpFitMissingKeyError(f"row {index} ({capability}) readback must be a string")

    return root


def load_mcp_fit(path: Path) -> dict[str, Any]:
    """Load and validate a JSON file at *path*.

    Raises :class:`McpFitError` on schema violations and :class:`OSError`
    on I/O errors.
    """
    raw = path.read_bytes()
    try:
        data: object = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise McpFitMissingKeyError(f"invalid JSON: {exc}") from exc
    return validate_mcp_fit_data(data)
