"""Shared connection/clock context for the runtime StateStore mixins."""

from __future__ import annotations

import sqlite3


class StateContext:
    """Dependencies mixins borrow from :class:`StateStore`.

    Mixins annotate ``_connection`` and may call the sequence/job
    guards; the concrete ``StateStore`` supplies the implementations.
    """

    _connection: sqlite3.Connection

    def _next_seq(self) -> int:
        raise NotImplementedError

    def _require_job(self, job_id: str) -> None:
        raise NotImplementedError


__all__ = ["StateContext"]
