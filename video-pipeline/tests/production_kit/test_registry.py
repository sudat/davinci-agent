"""Task 36: ChannelProductionKitV1 registry tests (TDD)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.production_kit.registry import (
    ProductionKitCountError,
    ProductionKitUnacceptedCapabilityError,
    ProductionKitUnknownCapabilityError,
    ProductionKitValidationError,
    load_kit,
    validate_kit,
)

VIDEO_PIPELINE = Path(__file__).resolve().parents[2]
KIT_PATH = VIDEO_PIPELINE / "config" / "production-kit" / "channel-kit-v1.json"


def _load_raw() -> dict[str, object]:
    return json.loads(KIT_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_real_kit_loads_and_validates() -> None:
    kit = load_kit()
    assert kit.kit_version == "1.0.0"
    assert len(kit.recipes) == 12
    ids = [r.recipe_id for r in kit.recipes]
    assert len(ids) == len(set(ids))
    # All bindings reference known capabilities and accepted_only is False
    for recipe in kit.recipes:
        assert recipe.preview_fixture
        assert recipe.expected_readback
        assert recipe.parameter_bounds
        assert recipe.provenance.origin
        assert recipe.provenance.license
        assert recipe.fallback in ("legacy_direct", "template_external", "manual")
        assert recipe.capability_binding.accepted_only is False
    # Reload via validate_kit dict path
    raw = _load_raw()
    validated = validate_kit(raw)
    assert len(validated.recipes) == 12


def test_missing_required_field_rejected() -> None:
    raw = _load_raw()
    broken = copy.deepcopy(raw)
    recipes = broken["recipes"]  # type: ignore[typeddict-item]
    assert isinstance(recipes, list)
    first = dict(recipes[0])  # type: ignore[arg-type]
    first.pop("parameter_bounds", None)
    recipes[0] = first
    with pytest.raises(ProductionKitValidationError):
        validate_kit(broken)


def test_unknown_capability_rejected() -> None:
    raw = _load_raw()
    broken = copy.deepcopy(raw)
    recipes = broken["recipes"]  # type: ignore[typeddict-item]
    assert isinstance(recipes, list)
    first = dict(recipes[0])  # type: ignore[arg-type]
    binding = dict(first["capability_binding"])  # type: ignore[arg-type]
    binding["capability"] = "does-not-exist-capability"
    first["capability_binding"] = binding
    recipes[0] = first
    with pytest.raises(ProductionKitUnknownCapabilityError, match="does-not-exist"):
        validate_kit(broken)


def test_accepted_only_with_not_accepted_status_rejected(tmp_path: Path) -> None:
    raw = _load_raw()
    broken = copy.deepcopy(raw)
    recipes = broken["recipes"]  # type: ignore[typeddict-item]
    assert isinstance(recipes, list)
    first = dict(recipes[0])  # type: ignore[arg-type]
    binding = dict(first["capability_binding"])  # type: ignore[arg-type]
    capability = str(binding["capability"])
    binding["accepted_only"] = True
    first["capability_binding"] = binding
    recipes[0] = first
    # The real matrix carries live Gate V43-0 statuses (task 11), so the
    # rejection path is proven against a doctored copy forcing not_available.
    mcp_fit = json.loads(
        (VIDEO_PIPELINE / "capabilities" / "v4.3" / "mcp-fit.json").read_text(encoding="utf-8")
    )
    for row in mcp_fit["capabilities"]:
        if row["capability"] == capability:
            row["status"] = "not_available"
    doctored = tmp_path / "mcp-fit.json"
    doctored.write_text(json.dumps(mcp_fit), encoding="utf-8")
    with pytest.raises(ProductionKitUnacceptedCapabilityError, match="not_available"):
        validate_kit(broken, mcp_fit_path=doctored)


def test_eleven_recipes_count_error() -> None:
    raw = _load_raw()
    broken = copy.deepcopy(raw)
    recipes = broken["recipes"]  # type: ignore[typeddict-item]
    assert isinstance(recipes, list)
    broken["recipes"] = recipes[:-1]
    assert len(broken["recipes"]) == 11  # type: ignore[arg-type]
    with pytest.raises((ProductionKitCountError, ProductionKitValidationError)):
        validate_kit(broken)
