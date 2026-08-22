"""Task 4: MCP fit matrix validator tests (TDD)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.toolchain.mcp_fit import (
    McpFitDuplicateCapabilityError,
    McpFitDuplicateFixtureError,
    McpFitInvalidFallbackError,
    McpFitInvalidStatusError,
    McpFitMissingKeyError,
    McpFitRowCountError,
    load_mcp_fit,
    validate_mcp_fit_data,
)

VIDEO_PIPELINE = Path(__file__).resolve().parents[2]
MCP_FIT = VIDEO_PIPELINE / "capabilities" / "v4.3" / "mcp-fit.json"


def _load_real() -> dict[str, object]:
    return json.loads(MCP_FIT.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_real_mcp_fit_validates_clean() -> None:
    data = _load_real()
    validate_mcp_fit_data(data)
    loaded = load_mcp_fit(MCP_FIT)
    assert loaded["schema_version"] == "mcp-fit-v1"
    caps = loaded["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    assert len(caps) == 22


def test_row_missing_fixture_raises_typed_error() -> None:
    data = _load_real()
    caps = data["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    broken = copy.deepcopy(data)
    broken_caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(broken_caps, list)
    first = dict(broken_caps[0])  # type: ignore[arg-type]
    first.pop("fixture", None)
    broken_caps[0] = first
    with pytest.raises(McpFitMissingKeyError, match="fixture"):
        validate_mcp_fit_data(broken)


def test_status_ok_is_rejected() -> None:
    data = _load_real()
    broken = copy.deepcopy(data)
    caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    row = dict(caps[0])  # type: ignore[arg-type]
    row["status"] = "ok"
    caps[0] = row
    with pytest.raises(McpFitInvalidStatusError, match="ok"):
        validate_mcp_fit_data(broken)


def test_row_count_21_raises_error() -> None:
    data = _load_real()
    broken = copy.deepcopy(data)
    caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    broken["capabilities"] = caps[:-1]
    with pytest.raises(McpFitRowCountError, match="22"):
        validate_mcp_fit_data(broken)


def test_duplicate_fixture_raises_error() -> None:
    data = _load_real()
    broken = copy.deepcopy(data)
    caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    first = dict(caps[0])  # type: ignore[arg-type]
    second = dict(caps[1])  # type: ignore[arg-type]
    second["fixture"] = first["fixture"]
    caps[1] = second
    with pytest.raises(McpFitDuplicateFixtureError, match="fixture"):
        validate_mcp_fit_data(broken)


def test_duplicate_capability_raises_error() -> None:
    data = _load_real()
    broken = copy.deepcopy(data)
    caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    first = dict(caps[0])  # type: ignore[arg-type]
    second = dict(caps[1])  # type: ignore[arg-type]
    second["capability"] = first["capability"]
    caps[1] = second
    with pytest.raises(McpFitDuplicateCapabilityError, match="capability"):
        validate_mcp_fit_data(broken)


def test_invalid_fallback_is_rejected() -> None:
    data = _load_real()
    broken = copy.deepcopy(data)
    caps = broken["capabilities"]  # type: ignore[typeddict-item]
    assert isinstance(caps, list)
    row = dict(caps[0])  # type: ignore[arg-type]
    row["fallback"] = "bogus"
    caps[0] = row
    with pytest.raises(McpFitInvalidFallbackError, match="bogus"):
        validate_mcp_fit_data(broken)
