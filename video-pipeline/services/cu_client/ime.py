"""Paired macOS input-source (IME) switch/restore for CU typing.

WHY this exists (measured 2026-09-06, sol-coords run): synthetic key
presses do NOT bypass the IME — CGEvents compose under the operator's
Japanese IME (Kotoeri): typing ``20`` into a focused text field produced
二〇, observed live. The IME interferes only while a text field has
focus; command shortcuts (Cmd+…) are unaffected. Driving text entry
while the CU agent works therefore requires switching the input source
to an ASCII layout for the duration and restoring the operator's
original source afterwards.

Mechanism (reused verbatim, not reinvented): the two embedded swift
snippets are copied verbatim from
``private/runtime/sol-coords-20260906/scripts/`` (``input_source.swift``
/ ``im_select.swift``), where they were used live;
``com.apple.keylayout.ABC`` was selected and read back successfully on
this machine. They run via a ``swift <file> [args]`` subprocess; the
snippet is written to the SYSTEM temp dir per call, never into the repo
tree at runtime.

Pair contract (Opus order 2026-09-06 01:35Z): switch and restore are a
PAIR — the restore runs in a ``finally`` on the success, exception, and
timeout paths (the same design as ``cu_window``'s reacquire). A timeout
that SIGKILLs the CHILD process group still leaves THIS driver process
alive to run the finally, so the wrapper belongs around the ``cu_window``
call, not inside the killed child.

Kill-path honesty: if THIS driver process itself is SIGKILLed mid-body,
the finally cannot run and the operator's input source stays switched —
the same residual limitation as the lease finally; documented, not
solved.

Restore-on-failed-switch policy: if the INITIAL switch fails
(:class:`~services.cu_client.errors.ImeSelectError`), the body never
runs (no yield) and NO restore is attempted — a refused select left the
source unchanged (swift exits 2 = no such id, 3 =
``TISSelectInputSource`` refused), so there is nothing of ours to undo.
The accepted corner: a select that returns ``noErr`` but fails the
readback leaves the source in an unknown state; we still do not
blind-restore — the error carries both ids and surfaces the drift
instead of masking it.

Never trust the switch API: after every select the CURRENT source is
read back and must match exactly (the same lesson as "import success ≠
assignment success"). The ORIGINAL source is read live before switching
and restored verbatim — never hardcoded: the operator's default may not
be Kotoeri.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from services.cu_client.errors import (
    ImeError,
    ImeReadError,
    ImeRestoreError,
    ImeSelectError,
)

ENGLISH_SOURCE_ID: Final = "com.apple.keylayout.ABC"
"""ASCII layout selected + read back live in the sol-coords run (2026-09-06)."""

_SWIFT_TIMEOUT_SECONDS: Final = 30.0
"""Generous: ``swift`` JIT-compiles the snippet; the first run can take seconds."""

_INPUT_SOURCE_SWIFT: Final = r"""import Carbon
import Foundation
if let src = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue() {
    func prop(_ key: CFString) -> String {
        guard let v = TISGetInputSourceProperty(src, key) else { return "?" }
        return Unmanaged<CFString>.fromOpaque(v).takeUnretainedValue() as String
    }
    let langs = TISGetInputSourceProperty(src, kTISPropertyInputSourceLanguages)
    var langStr = "?"
    if let langs = langs {
        let arr = Unmanaged<CFArray>.fromOpaque(langs).takeUnretainedValue() as NSArray
        langStr = arr.firstObject as? String ?? "?"
    }
    print("id:", prop(kTISPropertyInputSourceID), "| lang:", langStr, "| type:", prop(kTISPropertyInputSourceType), "| local:", prop(kTISPropertyLocalizedName))
}
"""

_IM_SELECT_SWIFT: Final = r"""import Carbon
import Foundation
let target = CommandLine.arguments[1] as CFString
let filter = [kTISPropertyInputSourceID: target] as CFDictionary
guard let cfArr = TISCreateInputSourceList(filter, false)?.takeRetainedValue(),
      CFArrayGetCount(cfArr) > 0 else {
    print("not found: \(target)"); exit(2)
}
let src = unsafeBitCast(CFArrayGetValueAtIndex(cfArr, 0), to: TISInputSource.self)
let st = TISSelectInputSource(src)
print(st == noErr ? "selected \(target)" : "failed: \(st)")
exit(st == noErr ? 0 : 3)
"""

_INPUT_LINE_RE: Final = re.compile(r"^id:\s*(\S+)\s*\|", re.MULTILINE)
"""Input-source ids are reverse-DNS (no spaces); unparseable output is an ImeReadError."""


def _run_swift(source: str, args: list[str]) -> tuple[int, str]:
    """Write the snippet to the system temp dir and run ``swift <file>``.

    Injectable seam: tests monkeypatch this module-level name with
    fakes; every function here resolves it at call time. Returns
    ``(exit_code, stdout)``; subprocess-level failures raise
    ``OSError`` / ``subprocess`` errors for the CALLER to convert into
    its typed error.
    """
    fd, path = tempfile.mkstemp(suffix=".swift", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
        completed = subprocess.run(
            ["swift", path, *args],
            capture_output=True,
            text=True,
            timeout=_SWIFT_TIMEOUT_SECONDS,
            check=False,
        )
        return completed.returncode, completed.stdout
    finally:
        Path(path).unlink()


def current_input_source() -> str:
    """Read the CURRENT input source id live (never a hardcoded default).

    Runs the proven read snippet and parses ``id: <source-id> | ...``.
    Any subprocess failure, nonzero exit, or unparseable output is a
    typed :class:`~services.cu_client.errors.ImeReadError`.
    """
    try:
        code, stdout = _run_swift(_INPUT_SOURCE_SWIFT, [])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImeReadError(f"input-source read failed to run: {exc}") from exc
    if code != 0:
        raise ImeReadError(f"input-source read exited {code}: {stdout.strip()}")
    match = _INPUT_LINE_RE.search(stdout)
    if match is None:
        raise ImeReadError(f"cannot parse input source from: {stdout.strip()!r}")
    return match.group(1)


def select_input_source(source_id: str) -> None:
    """Select ``source_id`` and VERIFY it took effect by reading back.

    The swift exit code (0 selected / 2 no such id / 3 select refused)
    is necessary but not sufficient: a zero exit is followed by a live
    read that must equal ``source_id`` exactly — the select's success
    return is never trusted (the "import success ≠ assignment success"
    lesson). A mismatch raises
    :class:`~services.cu_client.errors.ImeSelectError` carrying BOTH
    ids.
    """
    try:
        code, stdout = _run_swift(_IM_SELECT_SWIFT, [source_id])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImeSelectError(f"select {source_id!r} failed to run: {exc}") from exc
    if code != 0:
        raise ImeSelectError(f"select {source_id!r} exited {code}: {stdout.strip()}")
    readback = current_input_source()
    if readback != source_id:
        raise ImeSelectError(
            f"select {source_id!r} reported success but readback is {readback!r}"
        )


def _restore_source(original: str) -> None:
    """Restore + verify, normalizing every failure to ImeRestoreError."""
    try:
        select_input_source(original)
    except ImeError as exc:  # ImeSelectError (exit/mismatch) or ImeReadError (verify read)
        raise ImeRestoreError(f"restore to {original!r} failed: {exc}") from exc


def _assert_source(expected: str) -> None:
    """Read-only leave-state verification for the no-switch path (drift check)."""
    try:
        current = current_input_source()
    except ImeReadError as exc:
        raise ImeRestoreError(f"cannot verify {expected!r} after the body: {exc}") from exc
    if current != expected:
        raise ImeRestoreError(
            f"input source drifted to {current!r} during the body (expected {expected!r})"
        )


@contextmanager
def english_typing() -> Iterator[str]:
    """Guarantee an ASCII input source around a body; restore the original.

    Pair contract (see module docstring): record the original LIVE,
    switch to ``ENGLISH_SOURCE_ID`` unless already there, yield the
    original id, and in a FINALLY restore + readback-verify on the
    success, exception, and timeout paths. If the original is already
    English no switch is made, but the leave-state is still
    readback-verified — drift inside the body (the CU agent or an app
    switching the IME) surfaces as
    :class:`~services.cu_client.errors.ImeRestoreError` instead of
    passing silently. A failed INITIAL switch never yields and never
    restores (policy in the module docstring); a failed RESTORE raises
    ImeRestoreError and is never swallowed, even with a body exception
    in flight (the body error stays in the exception chain below the
    restore failure).
    """
    original = current_input_source()
    switched = original != ENGLISH_SOURCE_ID
    if switched:
        select_input_source(ENGLISH_SOURCE_ID)
    try:
        yield original
    finally:
        if switched:
            _restore_source(original)
        else:
            _assert_source(original)


__all__ = [
    "ENGLISH_SOURCE_ID",
    "current_input_source",
    "english_typing",
    "select_input_source",
]
