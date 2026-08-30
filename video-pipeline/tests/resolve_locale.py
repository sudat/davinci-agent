"""Locale isolation for tests that load the DaVinci Resolve native bridge.

The Resolve bridge's native module flips the process C locale to ``C``
(preferred encoding US-ASCII) when it loads. Leaked past a test boundary,
that flip poisons every later default-encoding text decode in the suite.
``native_bridge_locale`` restores the exact prior locale on every exit path,
so generator fixtures can wrap only their polluting setup portion and keep
their post-yield teardown semantics.
"""

from __future__ import annotations

import locale
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def native_bridge_locale() -> Iterator[None]:
    saved = locale.setlocale(locale.LC_ALL)
    try:
        yield
    finally:
        locale.setlocale(locale.LC_ALL, saved)
