"""gui_marker: exclusion file appears during the op, removed after, held refused.

Covers safeguard D (suda order 2026-09-06): entering creates the marker
with holder/started/purpose (readable by others), exiting removes it —
including on the exception path — and a second acquisition while held
raises GuiOccupiedError naming the holder. Uses tmp_path, never /tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.cu_client.errors import GuiOccupiedError
from services.cu_client.gui_marker import GuiMarker


def test_marker_appears_during_and_removed_after(tmp_path: Path) -> None:
    path = tmp_path / "fvp_gui_occupied.json"
    with GuiMarker(holder="sol", purpose="verify-A", path=path) as record:
        assert record["holder"] == "sol"
        assert record["purpose"] == "verify-A"
        assert "started" in record
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk == record  # others read exactly what we journal
    assert not path.exists()


def test_exception_path_still_removes(tmp_path: Path) -> None:
    path = tmp_path / "fvp_gui_occupied.json"
    with (
        pytest.raises(RuntimeError, match="mid-op"),
        GuiMarker(holder="sol", purpose="verify-B", path=path),
    ):
        raise RuntimeError("mid-op")
    assert not path.exists()


def test_second_acquisition_while_held_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fvp_gui_occupied.json"
    contender = GuiMarker(holder="zcode", purpose="other", path=path)
    with (
        GuiMarker(holder="sol", purpose="verify-C", path=path),
        pytest.raises(GuiOccupiedError, match="sol"),
    ):
        contender.__enter__()
    assert not path.exists()
