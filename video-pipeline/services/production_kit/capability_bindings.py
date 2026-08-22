"""Capability binding resolution — task 37 hard gate."""

from __future__ import annotations

from typing import Any

from services.production_kit.models import ProductionRecipeV1  # noqa: TC001


class CapabilityNotAcceptedError(Exception):
    """Raised when accepted_only binding points to non-accepted capability."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class CapabilityNotFoundError(Exception):
    """Raised when capability id is not present in mcp-fit."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _capability_map(mcp_fit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    caps = mcp_fit.get("capabilities")
    if not isinstance(caps, list):
        raise CapabilityNotFoundError("mcp_fit capabilities must be a list")
    out: dict[str, dict[str, Any]] = {}
    for row in caps:
        if not isinstance(row, dict):
            continue
        cap = row.get("capability")
        if isinstance(cap, str):
            out[cap] = row
    return out


def resolve_binding(
    recipe: ProductionRecipeV1,
    mcp_fit: dict[str, Any],
) -> dict[str, Any]:
    """Return the bound capability row; hard-gate on accepted_only.

    Raises:
        CapabilityNotFoundError: if the capability id is absent.
        CapabilityNotAcceptedError: if accepted_only is True and status != accepted.
    """
    cap_map = _capability_map(mcp_fit)
    cap_id = recipe.capability_binding.capability
    row = cap_map.get(cap_id)
    if row is None:
        raise CapabilityNotFoundError(
            f"capability {cap_id!r} not found in mcp-fit for recipe {recipe.recipe_id!r}"
        )
    if recipe.capability_binding.accepted_only:
        status = row.get("status")
        if status != "accepted":
            raise CapabilityNotAcceptedError(
                f"recipe {recipe.recipe_id!r} requires accepted capability "
                f"{cap_id!r} but status is {status!r}"
            )
    return row


__all__ = [
    "CapabilityNotAcceptedError",
    "CapabilityNotFoundError",
    "resolve_binding",
]
