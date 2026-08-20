"""Child helper: acquire a pty as the CONTROLLING terminal, then act.

Spawned with ``start_new_session=True`` (the child is a session leader).
Opening the tty path then acquires it as the session's controlling
terminal — automatically on macOS/Linux session leaders, with an explicit
``TIOCSCTTY`` fallback for anything else. This mirrors how a real login
attaches a terminal to a process, so ``record_operation``'s
controlling-terminal gate accepts it — unlike a self-owned pty, which is
never a controlling terminal and is always refused.

Results are written to a file and the child terminates via ``os._exit``:
a session leader's normal interpreter shutdown can hang at exit while
its controlling terminal's master is still held by the parent, so no
atexit/flush machinery is relied upon.

Modes (``python -m tests.approvals.ctty_child TTY MODE RESULT_FILE ...``):
  ``--ingress RESULT '<json>'``
      call ``record_operation(**json)`` with the acquired fd; write the
      draft (or refusal) JSON to the result file
  ``--stdin-runpy RESULT MODULE ARGS...``
      dup2 the fd onto stdin and run ``MODULE`` as ``__main__`` with
      ``ARGS`` (CLI subprocesses that use ``tty_fd=0``)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import runpy
import sys
import termios
from pathlib import Path

from services.approvals.ingress import (
    IngressRefusalError,
    controlling_terminal_name,
    record_operation,
)


def _attached(fd: int) -> bool:
    controlling = controlling_terminal_name()
    return controlling is not None and controlling == os.ttyname(fd)


def _write_result(path: str, payload: dict[str, object]) -> None:
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _acquire_controlling_tty(tty_path: str, result_path: str) -> int:
    fd = os.open(tty_path, os.O_RDWR)
    if not _attached(fd) and hasattr(termios, "TIOCSCTTY"):
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    if not _attached(fd):
        _write_result(
            result_path,
            {
                "ok": False,
                "code": "ctty-acquire-failed",
                "fd": os.ttyname(fd),
                "controlling": controlling_terminal_name(),
            },
        )
        os._exit(3)
    return fd


def _ingress_mode(result_path: str, payload: str, fd: int) -> None:
    arguments = json.loads(payload)
    try:
        draft = record_operation(tty_fd=fd, **arguments)
    except IngressRefusalError as error:
        _write_result(
            result_path, {"ok": False, "code": error.code, "detail": error.detail}
        )
        os._exit(0)
    _write_result(result_path, {"ok": True, "draft": draft.model_dump(mode="json")})
    os._exit(0)


def _stdin_runpy_mode(result_path: str, module: str, args: list[str], fd: int) -> None:
    os.dup2(fd, 0)
    sys.argv = [module, *args]
    code = 0
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exit_request:
        code = exit_request.code if isinstance(exit_request.code, int) else 1
        _write_result(result_path, {"ok": code == 0, "code": str(code)})
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main() -> None:
    if len(sys.argv) < 5:
        os._exit(2)
    tty_path, mode, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    fd = _acquire_controlling_tty(tty_path, result_path)
    if mode == "--ingress":
        _ingress_mode(result_path, sys.argv[4], fd)
    if mode == "--stdin-runpy":
        _stdin_runpy_mode(result_path, sys.argv[4], sys.argv[5:], fd)
    os._exit(2)


if __name__ == "__main__":
    main()
