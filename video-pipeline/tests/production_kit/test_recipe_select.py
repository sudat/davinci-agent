"""Task 37: taste-bounded recipe selection (TDD)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from services.production_kit.capability_bindings import (
    CapabilityNotAcceptedError,
    resolve_binding,
)
from services.production_kit.models import ChannelProductionKitV1, ProductionRecipeV1
from services.production_kit.provenance import provenance_summary
from services.production_kit.recipe_select import (
    NoRecipeError,
    RecipeSelection,
    TasteEvidence,
    apply_taste_params,
    select_recipe,
)
from services.production_kit.registry import load_kit
from services.reference_learning.models import DerivedTasteEntryV1


def _kit():
    return load_kit()


# ---------------------------------------------------------------------------
# (a) intent match → defaults + rationale
# ---------------------------------------------------------------------------


def test_intent_match_returns_defaults_and_rationale() -> None:
    kit = _kit()
    sel = select_recipe(kit, "color_look")
    assert isinstance(sel, RecipeSelection)
    assert sel.recipe.recipe_id == "color/channel-look"
    # defaults
    assert sel.resolved_params["look_strength"] == pytest.approx(0.6)
    assert sel.resolved_params["saturation"] == pytest.approx(1.0)
    assert "defaults" in sel.selection_rationale
    assert "recipe=color/channel-look" in sel.selection_rationale
    # apply_taste_params with None also returns defaults
    defaults = apply_taste_params(sel.recipe, None)
    assert defaults == sel.resolved_params


# ---------------------------------------------------------------------------
# (b) taste evidence within bounds → adjusted
# ---------------------------------------------------------------------------


def test_taste_evidence_within_bounds_adjusts_param() -> None:
    kit = _kit()
    # color/channel-look saturation 0.8-1.2, use 1.15 within bounds
    taste = [TasteEvidence(evidence_id="ev-1", param="saturation", value=1.15)]
    sel = select_recipe(kit, "color_look", taste_evidence=taste)
    assert sel.resolved_params["saturation"] == pytest.approx(1.15)
    # other param stays at default
    assert sel.resolved_params["look_strength"] == pytest.approx(0.6)
    assert "ev-1" in sel.selection_rationale
    assert "defaults" not in sel.selection_rationale

    # also via DerivedTasteEntryV1 statement parsing
    entry = DerivedTasteEntryV1(
        domain="color",
        statement="saturation 1.1",
        polarity="like",
        confidence=0.9,
        evidence_refs=("ev-2",),
        source_kind="reference_annotation",
    )
    sel2 = select_recipe(kit, "color_look", taste_evidence=[entry])
    assert sel2.resolved_params["saturation"] == pytest.approx(1.1)
    assert "ev-2" in sel2.selection_rationale


def test_taste_evidence_dict_form_within_bounds() -> None:
    kit = _kit()
    sel = select_recipe(
        kit,
        "color_look",
        taste_evidence=[{"evidence_id": "ev-3", "param": "look_strength", "value": 0.85}],
    )
    assert sel.resolved_params["look_strength"] == pytest.approx(0.85)
    assert "ev-3" in sel.selection_rationale


# ---------------------------------------------------------------------------
# (c) out-of-bounds → clipped + rationale records clip
# ---------------------------------------------------------------------------


def test_out_of_bounds_clipped_and_rationale_records() -> None:
    kit = _kit()
    # saturation max 1.2, demand 1.4 → clipped
    taste = [TasteEvidence(evidence_id="ev-clip-1", param="saturation", value=1.4)]
    sel = select_recipe(kit, "color_look", taste_evidence=taste)
    assert sel.resolved_params["saturation"] == pytest.approx(1.2)
    assert "clipped saturation 1.4→1.2 to bounds" in sel.selection_rationale
    assert "ev-clip-1" in sel.selection_rationale

    # apply_taste_params alone also clips
    recipe = next(r for r in kit.recipes if r.recipe_id == "color/channel-look")
    params = apply_taste_params(recipe, taste)
    assert params["saturation"] == pytest.approx(1.2)

    # Below min also clipped: saturation min 0.8, demand 0.1
    sel2 = select_recipe(
        kit,
        "color_look",
        taste_evidence=[TasteEvidence(evidence_id="ev-clip-2", param="saturation", value=0.1)],
    )
    assert sel2.resolved_params["saturation"] == pytest.approx(0.8)
    assert "clipped saturation 0.1→0.8 to bounds" in sel2.selection_rationale

    # Verify NO path produces out-of-bounds value
    for v in sel.resolved_params.values():
        assert 0.0 <= v <= 2.0  # generic sanity; real bounds check below
    for k, v in sel.resolved_params.items():
        bound = recipe.parameter_bounds[k]
        assert float(bound.min) <= v <= float(bound.max)


def test_taste_param_not_in_recipe_is_ignored() -> None:
    kit = _kit()
    sel = select_recipe(
        kit,
        "color_look",
        taste_evidence=[TasteEvidence(evidence_id="ev-9", param="nonexistent_param", value=99.0)],
    )
    # should remain defaults, no clip record for unknown param
    assert sel.resolved_params["saturation"] == pytest.approx(1.0)
    assert "clipped nonexistent_param" not in sel.selection_rationale


# ---------------------------------------------------------------------------
# (d) binding resolution with accepted_only + not accepted → error
# ---------------------------------------------------------------------------


def test_binding_accepted_only_rejects_not_accepted() -> None:
    kit = _kit()
    # Build a synthetic recipe with accepted_only=True
    base = next(r for r in kit.recipes if r.recipe_id == "color/channel-look")
    accepted_recipe = ProductionRecipeV1.model_validate(
        {
            **base.model_dump(),
            "capability_binding": {
                "capability": base.capability_binding.capability,
                "accepted_only": True,
            },
        }
    )
    # Live Gate V43-0 statuses (task 11) vary per capability, so the rejection
    # case is forced on a doctored copy instead of the real matrix bytes.
    mcp_fit_path = Path(__file__).resolve().parents[2] / "capabilities" / "v4.3" / "mcp-fit.json"
    mcp_fit = json.loads(mcp_fit_path.read_text(encoding="utf-8"))
    rejected_fit = copy.deepcopy(mcp_fit)
    for row in rejected_fit["capabilities"]:
        if row["capability"] == base.capability_binding.capability:
            row["status"] = "not_available"
    with pytest.raises(CapabilityNotAcceptedError, match="not_available"):
        resolve_binding(accepted_recipe, rejected_fit)
    # Without accepted_only, same row succeeds
    assert resolve_binding(base, mcp_fit)["capability"] == base.capability_binding.capability

    # accepted status passes when accepted_only
    accepted_mcp_fit = copy.deepcopy(mcp_fit)
    for row in accepted_mcp_fit["capabilities"]:
        if row["capability"] == base.capability_binding.capability:
            row["status"] = "accepted"
    # should not raise
    assert resolve_binding(accepted_recipe, accepted_mcp_fit)["status"] == "accepted"


# ---------------------------------------------------------------------------
# (e) no recipe for intent → NoRecipeError
# ---------------------------------------------------------------------------


def test_no_recipe_for_intent_raises() -> None:
    kit = _kit()
    with pytest.raises(NoRecipeError, match="no recipe for intent"):
        select_recipe(kit, "nonexistent_intent_xyz")
    with pytest.raises(NoRecipeError):
        select_recipe(kit, "")


# ---------------------------------------------------------------------------
# (f) provenance_summary includes license
# ---------------------------------------------------------------------------


def test_provenance_summary_includes_license() -> None:
    kit = _kit()
    recipe = next(r for r in kit.recipes if r.recipe_id == "color/channel-look")
    summary = provenance_summary(recipe)
    assert "license=" in summary
    assert recipe.provenance.license in summary
    assert "origin=" in summary
    assert recipe.provenance.origin in summary
    assert recipe.recipe_id in summary
    # one-line
    assert "\n" not in summary


# ---------------------------------------------------------------------------
# stale_state: selection determinism
# ---------------------------------------------------------------------------


def test_selection_determinism_same_inputs_same_bytes() -> None:
    kit = _kit()
    taste = [TasteEvidence(evidence_id="ev-determ", param="saturation", value=1.15)]
    sel1 = select_recipe(kit, "color_look", taste_evidence=taste)
    sel2 = select_recipe(kit, "color_look", taste_evidence=taste)
    assert sel1.model_dump_json() == sel2.model_dump_json()
    # also defaults determinism
    d1 = select_recipe(kit, "subtitle_track")
    d2 = select_recipe(kit, "subtitle_track")
    assert d1.model_dump_json() == d2.model_dump_json()
    assert d1.resolved_params == d2.resolved_params


# ---------------------------------------------------------------------------
# tie-break determinism
# ---------------------------------------------------------------------------


def test_tie_break_first_by_recipe_id_order() -> None:
    kit = _kit()
    # Create kit with duplicate semantic_intent but different recipe_id
    _ = next(r for r in kit.recipes if r.semantic_intent == "color_look")
    kit_json_path = (
        Path(__file__).resolve().parents[2] / "config" / "production-kit" / "channel-kit-v1.json"
    )
    dup_payload = copy.deepcopy(json.loads(kit_json_path.read_text(encoding="utf-8")))
    extra = copy.deepcopy(dup_payload["recipes"][10])
    extra["recipe_id"] = "color/aaa-look"
    extra["semantic_intent"] = "color_look"
    dup_payload["recipes"].append(extra)
    all_recipes = [ProductionRecipeV1.model_validate(r) for r in dup_payload["recipes"]]
    kit_dup = ChannelProductionKitV1.model_construct(
        kit_version="test", recipes=tuple(all_recipes)
    )
    # select should pick color/aaa-look as it sorts first
    sel = select_recipe(kit_dup, "color_look")
    assert sel.recipe.recipe_id == "color/aaa-look"
    assert "tie-break" in sel.selection_rationale
