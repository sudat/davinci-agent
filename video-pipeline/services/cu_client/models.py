"""Data models for the metacua-go Computer Use client (pin + result)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from services.contracts.primitives import StrictModel


class CuPin(StrictModel):
    """Boundary parse of ``config/toolchains/cu-metacua.pin.json``.

    ``binary_path`` is the ONLY place the environment-dependent absolute path
    to the metacua-go wrapper lives — never code (machines differ, and such
    paths must not enter the repository source). ``default_timeout_s`` is
    3600 in the committed pin because metacua-go's max-steps 400 + effort
    high means a legitimate goal can legitimately run for a very long time;
    a shorter default would kill legitimate work mid-operation, so callers
    who know their goal is short pass ``timeout_s`` explicitly.

    ``session_lookup_limit`` bounds the ``sessions --limit N`` lookup used to
    find OUR session record among possibly concurrent manual sessions.
    """

    schema_version: Literal["cu-pin-v1"]
    binary_path: str = Field(min_length=1)
    default_timeout_s: float = Field(gt=0)
    session_lookup_limit: int = Field(gt=0)


class CuResult(StrictModel):
    """Outcome of one metacua-go goal run.

    ``exit_code`` is ``None`` exactly when the run was killed by timeout
    (the process never exited on its own). ``goal_id`` / ``finish`` /
    ``trace_dir`` come from the post-run ``sessions`` lookup: ``trace_dir``
    points at ``~/.metacua/traces/<goal_id>/`` which lives OUTSIDE this
    repo, is external mutable state, may disappear, and is NOT a captured
    artifact — no copy mechanism exists by design (size), so nothing here
    implies durability we cannot guarantee.

    ``verified`` follows the observation contract: ``unverified`` means we
    did not look (未観測) — it is never a success claim; ``verified`` only
    after the caller's verifier said True; ``failed_verification`` when the
    verifier said False or raised (``verification_note`` carries why).
    """

    goal: str
    exit_code: int | None
    goal_id: str | None = None
    finish: bool | None = None
    trace_dir: Path | None = None
    stdout_tail: str
    stderr_tail: str
    elapsed_seconds: float
    verified: Literal["verified", "failed_verification", "unverified"]
    verification_note: str | None = None


__all__ = ["CuPin", "CuResult"]
