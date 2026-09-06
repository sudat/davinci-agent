"""Resolve-on-every-press element coordinates (safeguard B, suda order 2026-09-06).

Kills cause 6 (Opus 2026-09-06T08:30:40Z: 原因6 配置が変わると座標がずれる):
reused coordinates go stale when the layout shifts, and a stale click is a
silent cancel (the run5b/suda-eyewitness lesson). The rule is structural:
:func:`resolve_center` takes ONLY a live AX resolver callable plus the
expected labels and returns the element center resolved AT CALL TIME.
There is deliberately no storage parameter, no module-level cache, and no
save/load helper — caching is BANNED, not merely discouraged.

Why the ban is load-bearing (measured 2026-09-06, sol-input-mech run):
layout pinning was unproven — synthetic divider-drags and menu opens
produced no layout change, so a cached coordinate can only be detected as
stale AFTER it fails. Every verified-press workflow must therefore call
this (or the equivalent AX ledger path in the evidence scripts) on every
press, including re-presses after a panel state change (e.g. Inspector
collapsed vs expanded): the coordinates are expected to DIFFER between
calls, and each call lands via its own fresh resolution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any


def resolve_center(
    resolver: Callable[[Sequence[str]], dict[str, Any]],
    expected_labels: Sequence[str],
) -> tuple[float, float]:
    """Resolve the button center via a LIVE AX query; never a stored value.

    ``resolver`` is called NOW (e.g. the ``axpress_verified.swift`` ledger
    path that returns the chosen element's bbox string). It must return a
    mapping with a ``bbox`` entry shaped ``(x, y, w, h)`` in AX pixels and
    a truthy ``matched`` entry (the label-match gate: R4 of the verified
    workflow — an unmatched element is refused, never pressed). Any
    refusal from the resolver propagates to the caller (miss vs
    pressed-but-no-change triage, R5); this function adds no fallback.
    """
    resolution = resolver(expected_labels)
    if not resolution.get("matched"):
        raise ValueError(
            f"element resolution refused: {resolution.get('reason', 'label mismatch')}"
        )
    bbox = resolution["bbox"]
    x, y, w, h = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    return (x + w / 2.0, y + h / 2.0)


__all__ = ["resolve_center"]
