"""Chain-side director call ledger: one line per completed director call.

The initial real-episode chain bills nothing to the consultation selection
budget (that journal covers consultation/proposal-generation attempts only —
see ``consultation_selection_budget``); the chain's own director cost is
recorded here, next to ``run/selection-inputs.json``. Every completed
director call appends exactly one line — including the single bounded
contradiction retry's second call — so the ledger is the complete call
count, never just the final observation. This journal is runtime state,
NEVER an authoritative artifact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.foundation_io import canonical_model_bytes

DIRECTOR_CALLS_NAME: Final = "director-calls.jsonl"


class DirectorCallEntryV1(StrictModel):
    """One completed director call on the initial chain.

    ``attempt`` numbers the calls of one selection run (1, then 2 only on
    the bounded contradiction retry). A second-attempt line carries the
    first attempt's typed refusal in ``triggered_by_refusal`` so the retry
    is attributable without re-reading the outcome.
    """

    schema_version: Literal["director-call-ledger-v1"] = "director-call-ledger-v1"
    episode_id: str
    attempt: int = Field(ge=1, strict=True)
    request_hash: str
    served_by: str
    transport: str
    triggered_by_refusal: str | None = None
    created_at: str


def _ledger_path(run_dir: Path) -> Path:
    return run_dir / DIRECTOR_CALLS_NAME


def append_director_call(run_dir: Path, entry: DirectorCallEntryV1) -> None:
    path = _ledger_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")


def load_director_calls(run_dir: Path) -> list[DirectorCallEntryV1]:
    try:
        lines = _ledger_path(run_dir).read_bytes().splitlines()
    except OSError:
        return []
    return [DirectorCallEntryV1.model_validate_json(line) for line in lines]


def director_call_count(run_dir: Path) -> int:
    return len(load_director_calls(run_dir))


def now_stamp() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DIRECTOR_CALLS_NAME",
    "DirectorCallEntryV1",
    "append_director_call",
    "director_call_count",
    "load_director_calls",
    "now_stamp",
]
