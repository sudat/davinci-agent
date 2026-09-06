"""preflight page/selection/dialog check + correction against fakes.

Covers safeguard A (suda order 2026-09-06): the correct-state fast path
(no correction calls), the wrong-page correction (open_page + re-read +
journal-able record), the missing-selection re-click, failed corrections
raising PreflightError instead of passing quietly, and dialog
presence/absence expectations. No MCP, GUI, or Resolve is ever contacted.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.cu_client.errors import PreflightError
from services.cu_client.preflight import (
    PreflightSpec,
    SelectionSpec,
    run_preflight,
)


class FakeActions:
    """Fake PreflightActions with scripted probe answers + call recording."""

    def __init__(
        self,
        *,
        pages: list[str],
        selections: list[list[dict[str, Any]]],
        dialogs: dict[str, bool] | None = None,
    ) -> None:
        self._pages = list(pages)
        self._selections = list(selections)
        self._dialogs = dialogs or {}
        self.calls: list[str] = []

    def get_page(self) -> str:
        self.calls.append("get_page")
        return self._pages.pop(0)

    def open_page(self, page: str) -> None:
        self.calls.append(f"open_page:{page}")

    def selected_items(self) -> list[dict[str, Any]]:
        self.calls.append("selected_items")
        return self._selections.pop(0)

    def select_clip(self, target: SelectionSpec) -> str:
        self.calls.append("select_clip")
        return "AX hit timeline item at re-fetched coords"

    def dialog_showing(self, title_fragment: str) -> bool:
        self.calls.append(f"dialog_showing:{title_fragment}")
        return self._dialogs.get(title_fragment, False)


def test_clean_state_makes_no_correction_calls() -> None:
    actions = FakeActions(
        pages=["edit"],
        selections=[[{"track_type": "video", "track_index": 1, "item_index": 0}]],
        dialogs={"変更": False},
    )
    spec = PreflightSpec(
        page="edit",
        selection=SelectionSpec(track_type="video", track_index=1, item_index=0),
        dialog_closed="変更",
    )
    report = run_preflight(spec, actions)
    assert report.ok is True
    assert report.corrections == []
    assert report.page_corrected is None
    assert report.selection_corrected is None
    assert actions.calls == ["get_page", "selected_items", "dialog_showing:変更"]


def test_wrong_page_is_corrected_and_observed() -> None:
    actions = FakeActions(pages=["deliver", "edit"], selections=[[]])
    report = run_preflight(PreflightSpec(page="edit"), actions)
    assert report.ok is True
    assert report.page_before == "deliver"
    assert report.page_after == "edit"
    assert report.page_corrected == "deliver->edit"
    assert report.corrections == ["page deliver->edit"]
    assert actions.calls == ["get_page", "open_page:edit", "get_page"]


def test_failed_page_correction_raises_instead_of_passing() -> None:
    actions = FakeActions(pages=["deliver", "deliver"], selections=[[]])
    with pytest.raises(PreflightError, match="page correction failed"):
        run_preflight(PreflightSpec(page="edit"), actions)


def test_missing_selection_is_reclicked_and_verified() -> None:
    actions = FakeActions(
        pages=["edit"],
        selections=[[], [{"name": "edit-source.mov"}]],
    )
    report = run_preflight(
        PreflightSpec(page="edit", selection=SelectionSpec(name_contains="edit-source")),
        actions,
    )
    assert report.ok is True
    assert report.selection_ok is True
    assert report.selection_corrected is not None
    assert report.corrections == [
        "selection re-clicked (AX hit timeline item at re-fetched coords)"
    ]


def test_failed_selection_correction_raises_instead_of_passing() -> None:
    actions = FakeActions(pages=["edit"], selections=[[], []])
    with pytest.raises(PreflightError, match="selection correction failed"):
        run_preflight(PreflightSpec(selection=SelectionSpec(item_index=0)), actions)


def test_expected_open_dialog_absent_fails_the_flight() -> None:
    actions = FakeActions(pages=["edit"], selections=[[]], dialogs={})
    report = run_preflight(PreflightSpec(dialog_open="クリップの速度を変更"), actions)
    assert report.ok is False
    assert report.dialog_open_state is False


def test_expected_closed_dialog_open_fails_the_flight() -> None:
    actions = FakeActions(pages=["edit"], selections=[[]], dialogs={"変更": True})
    report = run_preflight(PreflightSpec(dialog_closed="変更"), actions)
    assert report.ok is False
    assert report.dialog_closed_state is False
