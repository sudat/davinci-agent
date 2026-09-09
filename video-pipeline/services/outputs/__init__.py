"""Per-output format independence (two enumerated outputs: landscape, vertical)."""

from services.outputs.geometry import (
    OutputGeometryV1,
    OutputId,
    OutputOrientation,
    geometry_for,
    normalize_output_id,
)

__all__ = [
    "OutputGeometryV1",
    "OutputId",
    "OutputOrientation",
    "geometry_for",
    "normalize_output_id",
]
