"""The Todo-58 overlay-live evidence report payload (schema v1)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.presentation.overlay_models import OverlayRenderedMedia

if TYPE_CHECKING:
    from services.presentation.overlay_models import (
        OverlayPlacementReadback,
        OverlaySection,
    )
    from services.presentation.overlay_render import AnchorCheck, RenderedOverlay


def _media_row(item_id: str, rendered: RenderedOverlay) -> OverlayRenderedMedia:
    return OverlayRenderedMedia(
        item_id=item_id,
        path=str(rendered.path),
        sha256=rendered.sha256,
        duration_frames=rendered.nb_frames,
        expected_rgb=rendered.expected_rgb,
    )


def _report_payload(  # noqa: PLR0913 (evidence bundle contract)
    *,
    section: OverlaySection,
    rendered: RenderedOverlay,
    readback: OverlayPlacementReadback,
    fusion: dict[str, object],
    checks: tuple[AnchorCheck, ...],
    render_path: Path,
) -> dict[str, object]:
    return {
        "schema_version": "overlay-live-report-v1",
        "passed": True,
        "rendered_presence": "verified",
        "fusion": fusion,
        "readback": readback.model_dump(mode="json"),
        "anchor_checks": [
            {
                "frame": check.anchor.frame_index,
                "expect_covered": check.anchor.expect_covered,
                "coverage_percent": check.coverage_percent,
                "passed": check.passed,
                "detail": check.detail,
            }
            for check in checks
        ],
        "overlay_media": {
            "path": str(rendered.path),
            "sha256": rendered.sha256,
            "pix_fmt": rendered.pix_fmt,
            "nb_frames": rendered.nb_frames,
            "expected_rgb": list(rendered.expected_rgb),
        },
        "render": {
            "output_path": str(render_path),
            "output_sha256": sha256_file(render_path),
        },
        "decisions": [row.model_dump(mode="json") for row in section.decisions],
    }


__all__ = ["_media_row", "_report_payload"]
