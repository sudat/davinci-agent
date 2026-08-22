"""Provenance summary — task 37."""

from __future__ import annotations

from services.production_kit.models import ProductionRecipeV1  # noqa: TC001


def provenance_summary(recipe: ProductionRecipeV1) -> str:
    """One-line license/origin/assets summary for build reports."""
    assets = recipe.provenance.assets
    assets_str = ", ".join(assets) if assets else "none"
    return (
        f"{recipe.recipe_id} origin={recipe.provenance.origin}"
        f" license={recipe.provenance.license} assets=[{assets_str}]"
    )


__all__ = ["provenance_summary"]
