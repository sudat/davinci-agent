# noqa: INP001 (evidence tree is not an importable package by design)
"""Raw typed MCP action calls for Task 4 probe tooling.

Light by design: importing this module pulls in nothing beyond the stdlib,
so the non-live render selfcheck stays free of the live client stack. The
protocols describe the only seam the probes use — an object whose client
answers raw ``(tool, action, params)`` MCP calls (the pinned McpClient in
live runs, a scripted fake in the selfcheck).
"""

from __future__ import annotations

import json
from typing import Protocol


class RenderClient(Protocol):
    """Anything answering raw MCP action calls (pinned McpClient or fake)."""

    def _call_action_json(
        self, tool: str, action: str, params: dict[str, object]
    ) -> object: ...


class RenderSession(Protocol):
    """A live session or selfcheck stand-in exposing a ``RenderClient``."""

    client: RenderClient


def raw(
    session: RenderSession, tool: str, action: str, params: dict[str, object]
) -> dict[str, object]:
    payload = session.client._call_action_json(tool, action, params)  # noqa: SLF001 (evidence capture seam: probes script the pinned client's raw action call so the ledger records exact vendor traffic)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{tool}.{action} non-dict: {payload!r}")  # noqa: TRY004 (response-shape guard on vendor output, not an argument type; RuntimeError keeps the probe failure family)
    return payload


def must(payload: dict[str, object], label: str) -> dict[str, object]:
    if payload.get("success") is False or isinstance(payload.get("error"), dict):
        raise RuntimeError(f"{label} failed: {json.dumps(payload)[:400]}")
    return payload


__all__ = ["RenderClient", "RenderSession", "must", "raw"]
