"""Loopback-only network guard for clean-room replay (Todo 67).

The guard is a ``sitecustomize.py`` injected via ``PYTHONPATH`` into every
replay child process: any external egress attempt (connect, DNS, non-loopback
create_connection) aborts the process with a typed marker so replay can
classify it as a hidden network release prevention. Loopback IPC stays
allowed (PRD 27: local-only bridge surfaces).
"""

from __future__ import annotations

import os
from pathlib import Path

NETWORK_MARKER = "RELEASE_REPLAY_NETWORK_BLOCKED"
GUARD_DIR_NAME = "network-guard"
CACHE_DIR_NAME = "uv-cache"
VENV_NAME = "venv"

GUARD_SOURCE = """\
import socket as _socket

_MARKER = "RELEASE_REPLAY_NETWORK_BLOCKED"
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _loopback(host):
    return not isinstance(host, str) or host in _LOOPBACK or host.startswith("127.")


def _egress(*where):
    raise SystemExit(_MARKER + ":" + ":".join(str(item) for item in where))


_connect = _socket.socket.connect
_connect_ex = _socket.socket.connect_ex


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if _loopback(host):
        return _connect(self, address)
    _egress("connect", host)


def _guarded_connect_ex(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if _loopback(host):
        return _connect_ex(self, address)
    _egress("connect_ex", host)


_create = _socket.create_connection
_getaddrinfo = _socket.getaddrinfo
_gethostbyname = _socket.gethostbyname


def _guarded_create(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else address
    if _loopback(host):
        return _create(address, *args, **kwargs)
    _egress("create_connection", host)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if _loopback(host):
        return _getaddrinfo(host, *args, **kwargs)
    _egress("getaddrinfo", host)


def _guarded_gethostbyname(host):
    if _loopback(host):
        return _gethostbyname(host)
    _egress("gethostbyname", host)


_socket.socket.connect = _guarded_connect
_socket.socket.connect_ex = _guarded_connect_ex
_socket.create_connection = _guarded_create
_socket.getaddrinfo = _guarded_getaddrinfo
_socket.gethostbyname = _guarded_gethostbyname
"""


def write_network_guard(out: Path) -> None:
    """Materialize the guard ``sitecustomize.py`` under ``out/network-guard``."""

    guard_dir = out / GUARD_DIR_NAME
    guard_dir.mkdir(parents=True, exist_ok=True)
    target = guard_dir / "sitecustomize.py"
    if not target.exists():
        target.write_text(GUARD_SOURCE)
        target.chmod(0o444)


def child_environment(out: Path) -> dict[str, str]:
    """Fresh, offline, no-bytecode child environment for every replay step."""

    environment = dict(os.environ)
    environment["UV_PROJECT_ENVIRONMENT"] = str(out / VENV_NAME)
    environment["UV_CACHE_DIR"] = str(out / CACHE_DIR_NAME)
    environment["UV_OFFLINE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(out / GUARD_DIR_NAME)
    environment.pop("PYTHONHOME", None)
    return environment


__all__ = [
    "CACHE_DIR_NAME",
    "GUARD_DIR_NAME",
    "GUARD_SOURCE",
    "NETWORK_MARKER",
    "VENV_NAME",
    "child_environment",
    "write_network_guard",
]
