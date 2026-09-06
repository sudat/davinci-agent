"""live_coords: every press resolves at call time; no cache exists.

Covers safeguard B (suda order 2026-09-06): the resolver callable runs on
EVERY call (two presses = two resolutions, each returning its own bbox —
the Inspector-collapsed-vs-expanded case), the center is the bbox center,
and a label-mismatch resolution is refused instead of pressed. No AX,
GUI, or Resolve is ever contacted: the resolver is a recording fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from services.cu_client.live_coords import resolve_center


class FakeResolver:
    """Scripted live AX resolver: pops one resolution per call, records calls."""

    def __init__(self, resolutions: list[dict[str, Any]]) -> None:
        self._resolutions = list(resolutions)
        self.calls: list[list[str]] = []

    def __call__(self, labels: Sequence[str]) -> dict[str, Any]:
        self.calls.append(list(labels))
        return self._resolutions.pop(0)


def test_each_call_resolves_fresh_coordinates() -> None:
    """Collapsed vs expanded panel: two presses, two live resolutions, differing centers."""
    resolver = FakeResolver(
        [
            {"matched": True, "bbox": (100.0, 200.0, 60.0, 20.0)},
            {"matched": True, "bbox": (100.0, 260.0, 60.0, 20.0)},
        ]
    )
    first = resolve_center(resolver, ["変更"])
    second = resolve_center(resolver, ["変更"])
    assert first == (130.0, 210.0)
    assert second == (130.0, 270.0)
    assert first != second
    assert len(resolver.calls) == 2  # no caching: the resolver ran twice


def test_label_mismatch_is_refused_never_pressed() -> None:
    resolver = FakeResolver([{"matched": False, "reason": "label"}])
    with pytest.raises(ValueError, match="refused"):
        resolve_center(resolver, ["変更"])
