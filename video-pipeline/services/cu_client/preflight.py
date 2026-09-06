"""Pre-GUI-operation state check & correction (safeguard A, suda order 2026-09-06).

Kills cause 7 (Opus 2026-09-06T08:30:40Z: 原因7 レンダー後にページが変わる):
the run5b lessons recorded "R inert on Deliver page (render leaves it
there)" — a render leaves Resolve on the Deliver page, so a GUI sequence
that assumes the Edit page fails silently afterwards. The fix is a
pre-flight that runs BEFORE any GUI operation: check the page, the clip
selection, and the expected dialog/panel state via API/AX probes, CORRECT
what is wrong (``open_page``, true-coords re-click), and only then let
the operation proceed.

Shape: :func:`run_preflight` takes an expectation spec plus an
``actions`` object (the live driver wires it to McpClient + metacua +
swift AX; tests fake it). It returns a journal-ready record of the
observed state AND every correction made — corrections are facts to log,
never silent side effects. A correction the post-check still shows as
wrong is a typed :class:`~services.cu_client.errors.PreflightError`
(not a quiet pass): "fixed" must be observed, never assumed (the same
lesson as import-success vs assignment-success).
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.cu_client.errors import PreflightError


class SelectionSpec(StrictModel):
    """The single clip that must be selected before the GUI operation.

    Match keys: ``track_type`` / ``track_index`` / ``item_index`` when the
    backend exposes them, ``name_contains`` otherwise (the AX Inspector
    header read or the ``get_selected_timeline_items`` name field). Only
    the keys SET are compared — unset keys are not evidence either way.
    """

    track_type: str | None = None
    track_index: int | None = None
    item_index: int | None = None
    name_contains: str | None = None


class PreflightSpec(StrictModel):
    """What the GUI operation assumes. Unset fields are not checked.

    ``dialog_open`` / ``dialog_closed`` take AX title fragments (e.g. the
    speed dialog's "クリップの速度を変更"): an expected-OPEN dialog that is
    absent is a MISSING state (the operation itself opens it — report,
    don't auto-create); an expected-CLOSED dialog that is open is reported
    for the caller to close deliberately.
    """

    page: str | None = None
    selection: SelectionSpec | None = None
    dialog_open: str | None = None
    dialog_closed: str | None = None


class PreflightActions(Protocol):
    """Live seams the driver implements (MCP + AX); tests substitute fakes."""

    def get_page(self) -> str:
        """Current Resolve page via ``resolve_control get_page``."""
        ...

    def open_page(self, page: str) -> None:
        """Move to ``page`` via ``resolve_control open_page``."""
        ...

    def selected_items(self) -> list[dict[str, Any]]:
        """Currently selected timeline items (API); empty means none."""
        ...

    def select_clip(self, target: SelectionSpec) -> str:
        """Re-select the clip (AX-derived TRUE clip coords click).

        Returns a short note describing HOW the click point was derived
        (AX element line or documented fallback) for the journal.
        """
        ...

    def dialog_showing(self, title_fragment: str) -> bool:
        """True when a window/dialog exposing ``title_fragment`` exists (AX)."""
        ...


class PreflightReport(StrictModel):
    """Observed state + corrections made. Journal this verbatim."""

    ok: bool
    page_before: str | None = None
    page_after: str | None = None
    page_corrected: str | None = None
    selection_ok: bool | None = None
    selection_corrected: str | None = None
    dialog_open_state: bool | None = None
    dialog_closed_state: bool | None = None
    corrections: list[str] = Field(default_factory=list)


def _selection_matches(target: SelectionSpec, item: dict[str, Any]) -> bool:
    """Every SET spec key must match; unset keys are not evidence."""
    checks: list[tuple[Any, Any]] = [
        (target.track_type, item.get("track_type")),
        (target.track_index, item.get("track_index")),
        (target.item_index, item.get("item_index")),
    ]
    for expected, actual in checks:
        if expected is not None and actual != expected:
            return False
    if target.name_contains is not None:
        name = str(item.get("name") or "")
        if target.name_contains not in name:
            return False
    return True


def _selection_ok(target: SelectionSpec, items: list[dict[str, Any]]) -> bool:
    return any(isinstance(item, dict) and _selection_matches(target, item) for item in items)


def run_preflight(spec: PreflightSpec, actions: PreflightActions) -> PreflightReport:
    """Check the assumed state; correct page + selection; report everything.

    Order: page first (a wrong page makes every later probe meaningless),
    then selection, then dialog/panel state. Each correction is re-read
    and a still-wrong post-check raises :class:`PreflightError` — the
    "corrected" claim is journaled only after it is observed.
    """
    corrections: list[str] = []
    ok = True

    page_before = actions.get_page()
    page_after = page_before
    page_corrected: str | None = None
    if spec.page is not None and page_before != spec.page:
        actions.open_page(spec.page)
        page_after = actions.get_page()
        if page_after != spec.page:
            raise PreflightError(
                f"page correction failed: expected {spec.page!r}, "
                f"was {page_before!r}, still {page_after!r} after open_page"
            )
        page_corrected = f"{page_before}->{page_after}"
        corrections.append(f"page {page_corrected}")

    selection_ok: bool | None = None
    selection_corrected: str | None = None
    if spec.selection is not None:
        if _selection_ok(spec.selection, actions.selected_items()):
            selection_ok = True
        else:
            note = actions.select_clip(spec.selection)
            if _selection_ok(spec.selection, actions.selected_items()):
                selection_ok = True
                selection_corrected = note
                corrections.append(f"selection re-clicked ({note})")
            else:
                raise PreflightError(
                    f"selection correction failed: {spec.selection!r} "
                    f"still not selected after re-click ({note})"
                )

    dialog_open_state: bool | None = None
    if spec.dialog_open is not None:
        dialog_open_state = actions.dialog_showing(spec.dialog_open)
        if not dialog_open_state:
            ok = False  # missing: the operation opens it; report, don't create
    dialog_closed_state: bool | None = None
    if spec.dialog_closed is not None:
        dialog_closed_state = not actions.dialog_showing(spec.dialog_closed)
        if not dialog_closed_state:
            ok = False  # open: caller must close it deliberately first

    # Reaching here means page + selection are established (failures raise
    # above); only dialog expectations can still fail the flight.
    return PreflightReport(
        ok=ok,
        page_before=page_before,
        page_after=page_after,
        page_corrected=page_corrected,
        selection_ok=selection_ok,
        selection_corrected=selection_corrected,
        dialog_open_state=dialog_open_state,
        dialog_closed_state=dialog_closed_state,
        corrections=corrections,
    )


__all__ = [
    "PreflightActions",
    "PreflightReport",
    "PreflightSpec",
    "SelectionSpec",
    "run_preflight",
]
