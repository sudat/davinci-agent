"""Recipe selection + taste bounding — task 37."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from services.contracts.primitives import Identifier, StrictModel
from services.production_kit.models import ChannelProductionKitV1, ProductionRecipeV1  # noqa: TC001

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NoRecipeError(Exception):
    """No recipe matches the requested semantic intent."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class TasteEvidenceError(Exception):
    """Malformed taste evidence."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


# ---------------------------------------------------------------------------
# Taste evidence model (bounded API surface)
# ---------------------------------------------------------------------------


class TasteEvidence(StrictModel):
    """Single taste-driven param adjustment with provenance citation."""

    evidence_id: Identifier
    param: str = Field(min_length=1)
    value: float


# ---------------------------------------------------------------------------
# RecipeSelection
# ---------------------------------------------------------------------------


class RecipeSelection(StrictModel):
    """Result of intent → recipe selection with bounded taste."""

    recipe: ProductionRecipeV1
    resolved_params: dict[str, float]
    selection_rationale: str


# ---------------------------------------------------------------------------
# Helpers: taste evidence normalization
# ---------------------------------------------------------------------------

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _iter_desired(  # noqa: C901, PLR0912, PLR0915
    recipe: ProductionRecipeV1,
    taste_evidence: Sequence[Any] | None,
) -> list[tuple[str, str, float]]:
    """Normalize taste_evidence into list of (param, evidence_id, desired)."""
    if not taste_evidence:
        return []
    out: list[tuple[str, str, float]] = []
    for item in taste_evidence:
        # Dict form: {evidence_id, param, value}  or {evidence_id, adjustments: {param: value}}
        if isinstance(item, dict):
            evid = item.get("evidence_id") or item.get("id") or item.get("evidence_ref")
            if not isinstance(evid, str) or not evid:
                continue
            # adjustments dict form
            if "adjustments" in item and isinstance(item["adjustments"], dict):
                for p, v in item["adjustments"].items():  # type: ignore[union-attr]
                    if isinstance(p, str) and isinstance(v, (int, float)):
                        out.append((p, evid, float(v)))
                continue
            param = item.get("param") or item.get("param_name")
            raw_val = item.get("value") if "value" in item else item.get("desired_value")
            if isinstance(param, str) and param and isinstance(raw_val, (int, float)):
                out.append((param, evid, float(raw_val)))
                continue
            # Derived entry dict shape: evidence_refs + statement
            if "evidence_refs" in item and "statement" in item:
                refs = item["evidence_refs"]
                stmt = item["statement"]
                if isinstance(refs, (list, tuple)) and refs and isinstance(stmt, str):
                    first_ref = refs[0]
                    ref_id = first_ref if isinstance(first_ref, str) else str(first_ref)
                    for p in recipe.parameter_bounds:
                        pat = re.compile(
                            rf"{re.escape(p)}\s*[:=]?\s*({_FLOAT_RE.pattern})"
                        )
                        m = pat.search(stmt)
                        if m:
                            try:
                                out.append((p, ref_id, float(m.group(1))))
                            except ValueError:
                                continue
                continue
            continue
        # TasteEvidence instance
        if isinstance(item, TasteEvidence):
            out.append((item.param, item.evidence_id, float(item.value)))
            continue
        # Generic object with evidence_id / param / value attrs
        evid_attr = getattr(item, "evidence_id", None)
        param_attr = getattr(item, "param", None)
        value_attr = getattr(item, "value", None)
        if (
            isinstance(evid_attr, str)
            and isinstance(param_attr, str)
            and isinstance(value_attr, (int, float))
        ):
            out.append((param_attr, evid_attr, float(value_attr)))
            continue
        # DerivedTasteEntryV1 duck-typing: domain, statement, evidence_refs
        evidence_refs = getattr(item, "evidence_refs", None)
        statement = getattr(item, "statement", None)
        if evidence_refs is not None and statement is not None:
            try:
                refs_seq = list(evidence_refs)  # type: ignore[arg-type]
            except TypeError:
                continue
            if not refs_seq or not isinstance(statement, str):
                continue
            ref_id = str(refs_seq[0])
            for p in recipe.parameter_bounds:
                pat = re.compile(rf"{re.escape(p)}\s*[:=]?\s*({_FLOAT_RE.pattern})")
                m = pat.search(statement)
                if m:
                    try:
                        out.append((p, ref_id, float(m.group(1))))
                    except ValueError:
                        continue
            continue
        # Unknown shape — ignore for resilience, but do not create bypass path
        continue
    return out


def _defaults(recipe: ProductionRecipeV1) -> dict[str, float]:
    return {k: float(v.default) for k, v in recipe.parameter_bounds.items()}


# ---------------------------------------------------------------------------
# apply_taste_params — the ONLY path to resolved_params
# ---------------------------------------------------------------------------


def apply_taste_params(
    recipe: ProductionRecipeV1,
    taste_evidence: Sequence[Any] | None = None,
) -> dict[str, float]:
    """Return resolved params clipped to bounds.

    Out-of-bounds values are clipped; clipping is reported via
    selection rationale in ``select_recipe``.  This function alone
    produces ``resolved_params`` — no other path may set a param
    outside bounds by construction.
    """
    resolved = _defaults(recipe)
    if not taste_evidence:
        return resolved
    desired_list = _iter_desired(recipe, taste_evidence)
    for param, _evid, desired in desired_list:
        bound = recipe.parameter_bounds.get(param)
        if bound is None:
            # Param not declared in recipe — ignore (no invention path)
            continue
        clipped = min(max(desired, float(bound.min)), float(bound.max))
        resolved[param] = clipped
    return resolved


def _apply_with_clips(
    recipe: ProductionRecipeV1,
    taste_evidence: Sequence[Any] | None,
) -> tuple[dict[str, float], list[str], list[str]]:
    resolved = _defaults(recipe)
    clips: list[str] = []
    evidence_ids: list[str] = []
    if not taste_evidence:
        return resolved, clips, evidence_ids
    desired_list = _iter_desired(recipe, taste_evidence)
    for param, evid, desired in desired_list:
        bound = recipe.parameter_bounds.get(param)
        if bound is None:
            continue
        clipped = min(max(desired, float(bound.min)), float(bound.max))
        evidence_ids.append(evid)
        if clipped != desired:
            clips.append(f"clipped {param} {desired}→{clipped} to bounds")
        resolved[param] = clipped
    return resolved, clips, evidence_ids


# ---------------------------------------------------------------------------
# select_recipe
# ---------------------------------------------------------------------------


def select_recipe(
    kit: ChannelProductionKitV1,
    semantic_intent: str,
    *,
    taste_evidence: Sequence[Any] | None = None,
) -> RecipeSelection:
    """Select recipe for semantic_intent with taste-bounded params.

    Matching: recipe.semantic_intent == requested intent exactly.
    Multiple matches → first by stable recipe_id order; rationale states tie-break.
    Rationale cites evidence ids or "defaults".
    """
    if not isinstance(semantic_intent, str) or not semantic_intent:
        raise NoRecipeError(f"no recipe for intent {semantic_intent!r}")

    matches = [r for r in kit.recipes if r.semantic_intent == semantic_intent]
    if not matches:
        raise NoRecipeError(f"no recipe for intent {semantic_intent!r}")

    matches_sorted = sorted(matches, key=lambda r: r.recipe_id)
    chosen = matches_sorted[0]

    tie_break = ""
    if len(matches_sorted) > 1:
        tie_break = (
            f" tie-break: selected {chosen.recipe_id} from "
            f"{len(matches_sorted)} matches by recipe_id order"
        )

    resolved, clips, evidence_ids = _apply_with_clips(chosen, taste_evidence)

    # Build rationale — must cite evidence ids or state defaults
    parts: list[str] = [f"intent={semantic_intent} recipe={chosen.recipe_id}"]
    if evidence_ids:
        # Deduplicate preserving order for rationale citation
        seen: set[str] = set()
        uniq: list[str] = []
        for eid in evidence_ids:
            if eid not in seen:
                seen.add(eid)
                uniq.append(eid)
        parts.append(f"evidence_ids=[{', '.join(uniq)}]")
    else:
        parts.append("defaults")

    if clips:
        parts.extend(clips)

    if tie_break:
        # Append without duplicating prefix already in tie_break
        parts.append(tie_break.strip())

    rationale = "; ".join(parts)

    return RecipeSelection(
        recipe=chosen,
        resolved_params=resolved,
        selection_rationale=rationale,
    )


__all__ = [
    "NoRecipeError",
    "RecipeSelection",
    "TasteEvidence",
    "TasteEvidenceError",
    "apply_taste_params",
    "select_recipe",
]
