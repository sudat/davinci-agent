"""ChannelProductionKitV1 registry — task 36."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from services.production_kit.models import EXPECTED_RECIPE_COUNT, ChannelProductionKitV1
from services.toolchain.mcp_fit import load_mcp_fit

DEFAULT_KIT_PATH: Path = (
    Path(__file__).resolve().parents[2] / "config" / "production-kit" / "channel-kit-v1.json"
)
DEFAULT_MCP_FIT_PATH: Path = (
    Path(__file__).resolve().parents[2] / "capabilities" / "v4.4" / "mcp-fit.json"
)

EXPECTED_COUNT: Final[int] = EXPECTED_RECIPE_COUNT


class ProductionKitError(Exception):
    """Base for all production-kit errors."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class ProductionKitValidationError(ProductionKitError):
    """Schema validation failed."""


class ProductionKitCountError(ProductionKitError):
    """Recipe count is not 12."""


class ProductionKitDuplicateIdError(ProductionKitError):
    """Duplicate recipe_id."""


class ProductionKitUnknownCapabilityError(ProductionKitError):
    """Capability binding references unknown capability."""


class ProductionKitUnacceptedCapabilityError(ProductionKitError):
    """accepted_only binding but capability status != accepted."""


def _mcp_capability_map(mcp_fit_path: Path) -> dict[str, str]:
    data = load_mcp_fit(mcp_fit_path)
    caps = data["capabilities"]  # type: ignore[typeddict-item]
    if not isinstance(caps, list):
        raise ProductionKitValidationError("mcp-fit capabilities must be a list")
    out: dict[str, str] = {}
    for row in caps:  # type: ignore[assignment]
        if not isinstance(row, dict):
            raise ProductionKitValidationError("mcp-fit row must be an object")
        cap = row["capability"]
        status = row["status"]
        if not isinstance(cap, str):
            raise ProductionKitValidationError("capability must be a string")
        if not isinstance(status, str):
            raise ProductionKitValidationError("status must be a string")
        out[cap] = status
    return out


def _validate_bindings(
    kit: ChannelProductionKitV1,
    mcp_fit_path: Path,
) -> None:
    cap_map = _mcp_capability_map(mcp_fit_path)
    for recipe in kit.recipes:
        cap = recipe.capability_binding.capability
        if cap not in cap_map:
            raise ProductionKitUnknownCapabilityError(
                f"recipe {recipe.recipe_id!r} references unknown capability {cap!r}"
            )
        if recipe.capability_binding.accepted_only:
            status = cap_map[cap]
            if status != "accepted":
                raise ProductionKitUnacceptedCapabilityError(
                    f"recipe {recipe.recipe_id!r} requires accepted capability "
                    f"{cap!r} but status is {status!r}"
                )


def _validate_dict_kit(
    payload: dict[str, Any],
) -> ChannelProductionKitV1:
    try:
        return ChannelProductionKitV1.model_validate(payload)
    except ValidationError as exc:
        msg = str(exc)
        if "kit_recipe_count" in msg:
            raise ProductionKitCountError(msg) from exc
        if "kit_duplicate_recipe_id" in msg:
            raise ProductionKitDuplicateIdError(msg) from exc
        raise ProductionKitValidationError(msg) from exc


def _validate_model_kit(kit: ChannelProductionKitV1) -> ChannelProductionKitV1:
    if len(kit.recipes) != EXPECTED_COUNT:
        raise ProductionKitCountError(
            f"kit must contain exactly 12 recipes, got {len(kit.recipes)}"
        )
    ids = [r.recipe_id for r in kit.recipes]
    if len(ids) != len(set(ids)):
        raise ProductionKitDuplicateIdError("kit recipe_ids must be unique")
    return kit


def validate_kit(
    kit: ChannelProductionKitV1 | dict[str, Any],
    *,
    mcp_fit_path: Path | None = None,
) -> ChannelProductionKitV1:
    """Validate a kit dict or model."""
    resolved_path = mcp_fit_path if mcp_fit_path is not None else DEFAULT_MCP_FIT_PATH
    if isinstance(kit, dict):
        validated = _validate_dict_kit(kit)
    elif isinstance(kit, ChannelProductionKitV1):
        validated = _validate_model_kit(kit)
    else:
        kind = type(kit).__name__
        raise ProductionKitValidationError(f"kit must be dict or model, got {kind}")

    try:
        _validate_bindings(validated, resolved_path)
    except (ProductionKitUnknownCapabilityError, ProductionKitUnacceptedCapabilityError):
        raise
    except ProductionKitError:
        raise
    except Exception as exc:  # pragma: no cover
        raise ProductionKitValidationError(str(exc)) from exc

    return validated


def load_kit(
    path: Path | None = None,
    *,
    mcp_fit_path: Path | None = None,
) -> ChannelProductionKitV1:
    """Load and validate kit from JSON file."""
    kit_path = path if path is not None else DEFAULT_KIT_PATH
    resolved_mcp = mcp_fit_path if mcp_fit_path is not None else DEFAULT_MCP_FIT_PATH
    try:
        raw = kit_path.read_bytes()
    except OSError as exc:
        raise ProductionKitValidationError(f"cannot read kit file {kit_path}: {exc}") from exc
    try:
        data: object = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProductionKitValidationError(f"invalid JSON in kit file: {exc}") from exc
    if not isinstance(data, dict):
        raise ProductionKitValidationError("kit root must be an object")
    return validate_kit(data, mcp_fit_path=resolved_mcp)  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_KIT_PATH",
    "DEFAULT_MCP_FIT_PATH",
    "ProductionKitCountError",
    "ProductionKitDuplicateIdError",
    "ProductionKitError",
    "ProductionKitUnacceptedCapabilityError",
    "ProductionKitUnknownCapabilityError",
    "ProductionKitValidationError",
    "load_kit",
    "validate_kit",
]
