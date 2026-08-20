"""Structured models for the local-listener security probe (Todo 66)."""

from __future__ import annotations

from typing import Literal

from services.contracts.primitives import StrictModel

PROBE_SCHEMA_VERSION = 1

type ListenerFamily = Literal["ipv4", "ipv6", "unix"]


class ListenerRecord(StrictModel):
    """One TCP listen socket observed on this machine.

    ``loopback`` is whether the socket is bound to a loopback address.
    ``pipeline`` is whether the owning process is one of this pipeline's
    services/bridge processes (see ``probe_local_listeners`` classification).
    """

    pid: int
    process: str
    address: str
    port: int | None
    family: ListenerFamily
    loopback: bool
    pipeline: bool


class ListenerReport(StrictModel):
    schema_version: int
    scanned_at_unix: int
    require_loopback: bool
    pass_verdict: bool
    verdict: str
    listeners: tuple[ListenerRecord, ...]
