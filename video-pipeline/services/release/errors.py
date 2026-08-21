"""Typed release-prevention errors (Todo 67).

Every release gate failure carries a typed code so a refused release is
auditable: the candidate is never silently altered or half-published.
"""

from __future__ import annotations

from typing import Final, Literal

ReleaseGateCode = Literal[
    "chat_or_timeline_dependency",
    "stale_hash",
    "forged_report",
    "failed_gate",
    "hidden_network",
    "duplicate_writer",
    "misleading_kpi_claim",
    "missing_release_input",
    "missing_h1_binding",
    "writable_candidate",
    "revoked_candidate",
    "uv-sha-required",
    "uv-hash-mismatch",
    "staging-ownership-required",
    "staging-ownership-mismatch",
    "min-passed-invalid",
]

GATE_CODE_DETAIL: Final[dict[str, str]] = {
    "chat_or_timeline_dependency": "chat history or an open Timeline is never a source of truth",
    "stale_hash": "candidate bytes or bindings drifted from the recorded hash",
    "forged_report": "a staged report disagrees with its own recomputed content",
    "failed_gate": "a required phase Gate is missing or not passed",
    "hidden_network": "clean-room replay attempted network access",
    "duplicate_writer": "another writer already owns the candidate path",
    "misleading_kpi_claim": "a staged KPI claim is unsupported by eligible real episodes",
    "missing_release_input": "a required release input is absent",
    "missing_h1_binding": "the total H1 operator-checkpoint binding is absent",
    "writable_candidate": "candidate files or directories are still writable",
    "revoked_candidate": "the candidate was revoked and must not be finalized",
}


class ReleaseGateError(Exception):
    """A typed release prevention; the candidate stays byte-retained."""

    def __init__(self, code: ReleaseGateCode, detail: str) -> None:
        joined = f"{code}: {detail}"
        super().__init__(joined)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


__all__ = [
    "GATE_CODE_DETAIL",
    "ReleaseGateCode",
    "ReleaseGateError",
]
