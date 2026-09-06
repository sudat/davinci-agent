"""ASCII-typed numeric entry under IME protection (safeguard C, suda order 2026-09-06).

Kills cause 5 (Opus 2026-09-06T08:30:40Z: 原因5 日本語入力が数値を変換した):
synthetic key events compose under the operator's Japanese IME — typing
``20`` into a focused field produced 二〇 live (sol-coords run,
2026-09-06). The paired switch/restore ``english_typing`` context manager
(``ime``, landed in 99e86b9) existed but only the subtitle procedure used
it; volume, color lift, and speed fields typed WITHOUT it and risked the
same conversion.

The rule: ALL numeric/ASCII typing sequences (volume, color lift, speed
fields — everywhere type-text/press-key characters go into a field) run
through :func:`type_ascii`. The body callback performs the actual key
sends; this wrapper guarantees the ASCII layout around it and restores
the operator's original source afterwards (the pair contract lives in
``ime`` — switch+readback-verify in, restore+readback-verify out).

Fail-closed on non-ASCII: a text containing non-ASCII characters is
refused BEFORE any switch happens (``ValueError``) — sending mojibake
into a value field and then "verifying" it is worse than not sending.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from services.cu_client.ime import english_typing


def type_ascii(text: str, send: Callable[[str], Any]) -> dict[str, str]:
    """Type ``text`` with the IME forced to an ASCII layout; journal the pair.

    ``send`` does the real key emission (e.g. metacua type-text plus Tab)
    and runs INSIDE the ``english_typing`` body, so the layout is ASCII
    for exactly the keystrokes and restored in the finally. Returns a
    journal-ready record: the original source id (proves the restore
    target was read live) and the text sent. A body exception propagates
    AFTER the restore (the ``ime`` pair contract), never instead of it.
    """
    if not text.isascii():
        raise ValueError(
            f"refusing non-ASCII numeric entry {text!r}: "
            "route prose through the subtitle procedure, not this path"
        )
    with english_typing() as original:
        send(text)
    return {"original_source": original, "text_sent": text, "layout": "ascii"}


__all__ = ["type_ascii"]
