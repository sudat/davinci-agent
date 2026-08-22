"""Typed cockpit errors — every HTTP failure is a structured {error: {code, detail}}."""

from __future__ import annotations


class CockpitError(Exception):
    """Base typed cockpit failure carrying a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class CockpitNotFoundError(CockpitError):
    """Typed 404 (unknown episode, absent preview, unknown approval record)."""


class CockpitConflictError(CockpitError):
    """Typed 409 (episode already exists, brief no longer a draft)."""


class CockpitUnprocessableError(CockpitError):
    """Typed 422 (bad source folder, unreadable reference, broken state store)."""


class CockpitBindError(CockpitError):
    """Typed refusal for any non-loopback bind host (factory and run())."""


__all__ = [
    "CockpitBindError",
    "CockpitConflictError",
    "CockpitError",
    "CockpitNotFoundError",
    "CockpitUnprocessableError",
]
