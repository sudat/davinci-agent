"""Subtitle capability ladder selection (task 33; PRD §9.3).

Selection is a PURE function of the mcp-fit matrix row for
``subtitle-capability``: ``accepted`` -> ``native_text_plus`` first, otherwise
the external ASS/SRT render rung (the legacy mov_text post-render path in
``services/resolve_bridge/fixed_presentation_subtitle.py`` stays available as
that rung's fallback; it is never modified or deleted). ``styled_template``
sits on the ladder between them for future matrix-driven promotion; ``manual``
is the terminal rung.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from services.creative_plan.subtitle_models import PATH_ORDER, SubtitleCapabilityPathV1
from services.toolchain.mcp_fit import VALID_STATUSES, load_mcp_fit

__all__ = [
    "PATH_ORDER",
    "SUBTITLE_CAPABILITY",
    "SubtitlePlanError",
    "load_matrix_subtitle_status",
    "select_subtitle_path",
]

SUBTITLE_CAPABILITY: Final = "subtitle-capability"
_MATRIX_PATH: Final = Path(__file__).resolve().parents[2] / "capabilities" / "v4.4" / "mcp-fit.json"


class SubtitlePlanError(ValueError):
    """Typed refusal from the subtitle pipeline (never a silent partial)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def select_subtitle_path(status: str) -> SubtitleCapabilityPathV1:
    """Map the mcp-fit matrix status onto the ordered capability ladder."""
    if status not in VALID_STATUSES:
        raise SubtitlePlanError(
            "invalid-matrix-status", f"unknown subtitle-capability status {status!r}"
        )
    if status == "accepted":
        selected = "native_text_plus"
        note = "subtitle-capability accepted by fixture: native Text+ rung ordered first"
    else:
        selected = "external_ass_srt"
        note = (
            f"subtitle-capability status {status!r}: external ASS/SRT render rung selected "
            "(mov_text post-render path remains the fallback there)"
        )
    return SubtitleCapabilityPathV1(
        ordered_paths=PATH_ORDER,
        selected=selected,
        matrix_capability=SUBTITLE_CAPABILITY,
        matrix_status=status,
        note=note,
    )


def load_matrix_subtitle_status(path: Path | None = None) -> str:
    """Read the subtitle-capability row status from the mcp-fit matrix."""
    resolved = path if path is not None else _MATRIX_PATH
    try:
        document = load_mcp_fit(resolved)
    except (OSError, ValueError) as error:
        raise SubtitlePlanError("matrix-unreadable", str(error)) from error
    rows = document.get("capabilities")
    if not isinstance(rows, list):
        raise SubtitlePlanError("matrix-row-missing", "matrix document carries no capabilities")
    for row in rows:
        if isinstance(row, dict) and row.get("capability") == SUBTITLE_CAPABILITY:
            status = row.get("status")
            if not isinstance(status, str):
                raise SubtitlePlanError("invalid-matrix-status", "status must be a string")
            return status
    raise SubtitlePlanError("matrix-row-missing", f"no {SUBTITLE_CAPABILITY} row in {resolved}")
