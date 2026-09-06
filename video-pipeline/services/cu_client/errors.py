"""Typed errors for the metacua-go Computer Use client package.

Every error raised by ``services.cu_client`` is a :class:`CuClientError`
subclass, so callers can catch the package boundary with one type.

DESIGN DEVIATION (recorded): the original three-error design had a
``CuTimeoutError``. It is intentionally FOLDED INTO :class:`CuResult`
(refinement 2, approved 2026-09-05): raising on timeout would prevent
recording the interruption fact — ``exit_code=None`` plus a
``verification_note`` of ``interrupted: killed by timeout after Ns; external
GUI state may be mid-operation`` — and "didn't look" and "may have
interrupted mid-operation" are different facts that must never collapse
into one plain ``unverified``. There is therefore no timeout exception type
here, by design.

The ``Ime*`` group (Opus order 2026-09-06 01:35Z) covers the paired
macOS input-source switch/restore helper (``ime``); it also subclasses
:class:`CuClientError` so the one-type package boundary still holds.
"""

from __future__ import annotations


class CuClientError(Exception):
    """Base class for every typed error raised by services.cu_client."""


class CuLaunchError(CuClientError):
    """The pinned binary cannot be launched.

    The pin config is missing/unreadable/invalid (including a missing
    ``binary_path`` key), the binary does not exist, is not executable, or
    the spawn itself failed.
    """


class CuTraceCollectionError(CuClientError):
    """The post-run ``sessions`` lookup failed.

    The lookup output was invalid JSON, not a session list, exited non-zero,
    or contained no session whose goal string EQUALS the goal we sent (an
    exact-match miss, not a nearest-match guess).
    """


class CuLeaseError(CuClientError):
    """A cu_window lease handoff step failed (renew/release/reacquire).

    The window fails fast on any lease refusal — it never waits for another
    holder — because a lost writer role must surface immediately, not after
    a timeout.
    """


class ImeError(CuClientError):
    """Base for the paired input-source (IME) switch/restore failures (``ime``)."""


class ImeReadError(ImeError):
    """Reading the CURRENT input source failed.

    The swift read could not run, exited nonzero, or its output could not
    be parsed — a missing id is a fact to surface, never a value to guess.
    """


class ImeSelectError(ImeError):
    """Selecting an input source failed.

    The swift select exited nonzero (2 = no such source id, 3 =
    ``TISSelectInputSource`` refused), OR it reported success but the
    readback id did not match the requested one — the select's success
    return is never trusted, so a mismatch carries both ids.
    """


class ImeRestoreError(ImeError):
    """Restoring the original input source failed or could not be verified.

    Raised from the pair's ``finally`` and never swallowed — even when a
    body exception is already in flight (the body error stays in the
    exception chain below the restore failure, it is not lost).
    """


__all__ = [
    "CuClientError",
    "CuLaunchError",
    "CuLeaseError",
    "CuTraceCollectionError",
    "ImeError",
    "ImeReadError",
    "ImeRestoreError",
    "ImeSelectError",
]
