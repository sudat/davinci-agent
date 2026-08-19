"""The Phase-3 styled presentation section of a Resolve Package (Todo 57).

The package carries the styled presentation VERBATIM — style_id per cue,
titled items with registry asset refs and their fact-evidence refs — after
re-verifying, from the package's own inputs, that the styled table is bound
to exactly this IR and carries its cue text and timing verbatim. Any drift
surfaces as the typed ``styled-presentation-drift`` package error before a
build starts. Resolve-specific application of styles stays in Todo-58
territory; this section is descriptive, NLE-neutral.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.presentation.styling import verify_styled_ir_binding
from services.resolve_adapter.errors import (
    STYLED_PRESENTATION_DRIFT,
    PackageCompileError,
)

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.presentation.styling_models import StyledPresentation


def attach_styled_presentation(
    styled: StyledPresentation, ir: TimelineIrProduction
) -> StyledPresentation:
    """Verify the styled table against this IR and return it for the package."""

    try:
        verify_styled_ir_binding(styled, ir)
    except ValueError as error:
        raise PackageCompileError(
            STYLED_PRESENTATION_DRIFT,
            f"styled presentation no longer binds to the IR: {error}",
        ) from error
    return styled


__all__ = ["attach_styled_presentation"]
