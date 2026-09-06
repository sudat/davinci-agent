"""Single-file screen-operation exclusion marker (safeguard D, suda order 2026-09-06).

Kills cause 4 (Opus 2026-09-06T08:30:40Z: 原因4 前面を他アプリに奪われた):
another agent (ZCode etc.) or suda touching the Mac mid-sequence steals
focus and turns clicks into cancels — the sol-matrix ⑩ session measured
exactly this (external Zoom reset 1.35→1.0 under contention, cause
unattributable). suda already coordinates verbally ("Mac使ってます /
空いてるよ"); this makes the same signal machine-readable.

Deliberately NOT a framework: one JSON file, one context manager.
Before screen operations the holder creates
``/tmp/fvp_gui_occupied.json`` with ``{holder, started, purpose}`` and
removes it in a finally. A second acquisition while the file exists is
refused with :class:`~services.cu_client.errors.GuiOccupiedError`
carrying the current holder (fail fast — never wait on a human's Mac).
Stale markers are the holder's honesty problem: ``started`` is a real
ISO timestamp so a reader can judge staleness itself; there is no
auto-expiry that could mask a live operation.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from types import TracebackType
from typing import Final

from services.cu_client.errors import GuiOccupiedError

DEFAULT_MARKER_PATH: Final = Path("/tmp/fvp_gui_occupied.json")  # noqa: S108
"""Well-known path other agents and suda's future tooling can read."""


def _read_marker(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class GuiMarker:
    """Exclusion marker: create on entry, remove in the finally, refuse held."""

    def __init__(
        self,
        *,
        holder: str,
        purpose: str,
        path: Path | str = DEFAULT_MARKER_PATH,
    ) -> None:
        self._holder = holder
        self._purpose = purpose
        self._path = Path(path)
        self.record: dict[str, str] = {}

    def __enter__(self) -> dict[str, str]:
        existing = _read_marker(self._path)
        if existing is not None:
            raise GuiOccupiedError(
                f"screen held by {existing.get('holder', '?')} "
                f"since {existing.get('started', '?')} "
                f"({existing.get('purpose', '?')}); refusing {self._holder}"
            )
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.record = {
            "holder": self._holder,
            "started": started,
            "purpose": self._purpose,
        }
        self._path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        return dict(self.record)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()


__all__ = ["DEFAULT_MARKER_PATH", "GuiMarker"]
