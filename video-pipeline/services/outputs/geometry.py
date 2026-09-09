"""Per-output format independence: the vertical variant (工程5 BACKEND).

Two enumerated outputs ONLY (YAGNI — no generic multi-output framework):

- ``landscape`` — 1920x1080, the frozen values (byte-identical behavior).
- ``vertical`` — 1080x1920, an independent chain sharing time coordinates.

Time coordinates are canvas-agnostic, so analysis/conform/mezzanine are
shared untouched; only geometry (canvas sizes, px math, render settings)
is parameterized per output.

Single-writer decision: the existing episode runner lock already serializes
all work within one episode, so per-output chains are serialized by the
same lock — no new lock is introduced. Each output's chain (sealed log +
versions index) lives at its own paths, so a vertical build never writes
the landscape files; ``landscape`` keeps the LEGACY paths so existing
episodes and records read unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes

OutputId = Literal["landscape", "vertical"]
OutputOrientation = Literal["landscape", "portrait"]

DEFAULT_OUTPUT_ID: Final = "landscape"

LANDSCAPE_WIDTH: Final = 1920
LANDSCAPE_HEIGHT: Final = 1080
VERTICAL_WIDTH: Final = 1080
VERTICAL_HEIGHT: Final = 1920

PREVIEW_LANDSCAPE_WIDTH: Final = 640
PREVIEW_LANDSCAPE_HEIGHT: Final = 360
PREVIEW_VERTICAL_WIDTH: Final = 360
PREVIEW_VERTICAL_HEIGHT: Final = 640

OUTPUTS_FILE_NAME: Final = "outputs.json"


class OutputGeometryV1(StrictModel):
    """The deliverable canvas of one enumerated output."""

    output_id: OutputId
    orientation: OutputOrientation
    width: int
    height: int

    @model_validator(mode="after")
    def require_pinned_dimensions(self) -> OutputGeometryV1:
        pinned = {"landscape": (1920, 1080, "landscape"), "vertical": (1080, 1920, "portrait")}
        width, height, orientation = pinned[self.output_id]
        if (self.width, self.height, self.orientation) != (width, height, orientation):
            raise PydanticCustomError(
                "geometry_mismatch",
                "{output_id} is pinned to {width}x{height} {orientation}",
                {"output_id": self.output_id, "width": width, "height": height,
                 "orientation": orientation},
            )
        return self

    @property
    def is_portrait(self) -> bool:
        return self.width < self.height


LANDSCAPE_GEOMETRY: Final = OutputGeometryV1(
    output_id="landscape", orientation="landscape", width=1920, height=1080
)
VERTICAL_GEOMETRY: Final = OutputGeometryV1(
    output_id="vertical", orientation="portrait", width=1080, height=1920
)


def geometry_for(output_id: OutputId) -> OutputGeometryV1:
    """The pinned geometry of one enumerated output."""
    if output_id == "vertical":
        return VERTICAL_GEOMETRY
    return LANDSCAPE_GEOMETRY


def normalize_output_id(value: str | None) -> OutputId:
    """Optional request field → output (None/"" means the landscape default)."""
    if value is None or value == "":
        return DEFAULT_OUTPUT_ID
    if value == "landscape":
        return "landscape"
    if value == "vertical":
        return "vertical"
    raise ValueError(f"unknown output_id {value!r}; expected 'landscape' or 'vertical'")


class OutputRegistryV1(StrictModel):
    """The registered outputs of one episode (default: landscape only)."""

    schema_version: Literal["episode-outputs-v1"]
    outputs: tuple[OutputGeometryV1, ...]

    @model_validator(mode="after")
    def require_landscape_present(self) -> OutputRegistryV1:
        ids = [output.output_id for output in self.outputs]
        if "landscape" not in ids or len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "outputs_invalid",
                "the registry must hold landscape exactly once plus unique outputs",
                {},
            )
        return self


def default_registry() -> OutputRegistryV1:
    return OutputRegistryV1(schema_version="episode-outputs-v1", outputs=(LANDSCAPE_GEOMETRY,))


def outputs_path(episode_dir: Path) -> Path:
    return episode_dir / OUTPUTS_FILE_NAME


def load_registry(episode_dir: Path) -> OutputRegistryV1:
    """The episode registry; absent file means the landscape default."""
    path = outputs_path(episode_dir)
    if not path.is_file():
        return default_registry()
    return OutputRegistryV1.model_validate_json(path.read_bytes())


def register_output(episode_dir: Path, output_id: OutputId) -> tuple[OutputRegistryV1, bool]:
    """Idempotent registration: returns (registry, created_new)."""
    try:
        registry = load_registry(episode_dir)
    except ValueError as error:
        raise OutputRegistryError("outputs-unreadable", str(error)) from error
    if any(output.output_id == output_id for output in registry.outputs):
        return registry, False
    updated = OutputRegistryV1(
        schema_version="episode-outputs-v1",
        outputs=(*registry.outputs, geometry_for(output_id)),
    )
    atomic_write(outputs_path(episode_dir), canonical_model_bytes(updated))
    return updated, True


class OutputRegistryError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def review_store_relatives(output_id: OutputId) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Cockpit (log, plan_dir) relatives; landscape keeps the LEGACY paths."""
    if output_id == "vertical":
        return ("review", "events-vertical.jsonl"), ("review", "store-vertical")
    return ("review", "events.jsonl"), ("review", "store")


def chain_store_relatives(output_id: OutputId) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Chain run/ (log, plan_dir) relatives; landscape keeps the LEGACY paths."""
    if output_id == "vertical":
        return (
            ("run", "review-store-vertical", "events.jsonl"),
            ("run", "review-store-vertical"),
        )
    return (("run", "review-store", "events.jsonl"), ("run", "review-store"))


def preview_relatives(output_id: OutputId) -> tuple[str, ...]:
    """Preview file relatives; landscape keeps the LEGACY preview.mp4."""
    if output_id == "vertical":
        return ("previews", "preview-vertical.mp4")
    return ("previews", "preview.mp4")


def preview_trace_relatives(output_id: OutputId) -> tuple[str, ...]:
    if output_id == "vertical":
        return ("previews", "preview-vertical-trace.json")
    return ("previews", "preview-trace.json")


def approvals_relatives(output_id: OutputId) -> tuple[str, ...]:
    """Approval ledger relatives; landscape keeps the LEGACY records.jsonl."""
    if output_id == "vertical":
        return ("approvals", "records-vertical.jsonl")
    return ("approvals", "records.jsonl")


def preview_size_for(output_id: OutputId) -> tuple[int, int]:
    """Low-resolution preview canvas: 640x360 landscape, 360x640 vertical."""
    if output_id == "vertical":
        return (PREVIEW_VERTICAL_WIDTH, PREVIEW_VERTICAL_HEIGHT)
    return (PREVIEW_LANDSCAPE_WIDTH, PREVIEW_LANDSCAPE_HEIGHT)


def subtitle_chars_for_output(base_chars_per_line: int, output_id: OutputId) -> int:
    """Cue line-wrap width scaled by canvas width ratio (landscape unchanged)."""
    if output_id == "landscape":
        return base_chars_per_line
    scaled = (base_chars_per_line * VERTICAL_WIDTH) // LANDSCAPE_WIDTH
    return max(1, scaled)


def scale_px_for_output(px: int, output_id: OutputId) -> int:
    """Landscape-authored px value scaled to the output canvas width."""
    if output_id == "landscape":
        return px
    return max(1, (px * VERTICAL_WIDTH) // LANDSCAPE_WIDTH)


__all__ = [
    "DEFAULT_OUTPUT_ID",
    "LANDSCAPE_GEOMETRY",
    "LANDSCAPE_HEIGHT",
    "LANDSCAPE_WIDTH",
    "OUTPUTS_FILE_NAME",
    "PREVIEW_LANDSCAPE_HEIGHT",
    "PREVIEW_LANDSCAPE_WIDTH",
    "PREVIEW_VERTICAL_HEIGHT",
    "PREVIEW_VERTICAL_WIDTH",
    "VERTICAL_GEOMETRY",
    "VERTICAL_HEIGHT",
    "VERTICAL_WIDTH",
    "OutputGeometryV1",
    "OutputId",
    "OutputOrientation",
    "OutputRegistryError",
    "OutputRegistryV1",
    "approvals_relatives",
    "chain_store_relatives",
    "default_registry",
    "geometry_for",
    "load_registry",
    "normalize_output_id",
    "outputs_path",
    "preview_relatives",
    "preview_size_for",
    "preview_trace_relatives",
    "register_output",
    "review_store_relatives",
    "scale_px_for_output",
    "subtitle_chars_for_output",
]
