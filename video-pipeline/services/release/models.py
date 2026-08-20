"""Release candidate layout, identity, and total H1 binding models (Todo 67).

Layout under the candidate root::

    manifest.json            (excluded from entries; candidate_id = sha256 bytes)
    identity.json            release identity (covered by the manifest)
    source/                  Git snapshot of ``video-pipeline`` at FINAL_SHA
    inputs/
      plan.md                          the plan file
      start-work-ledger.jsonl          suffix through todo-66 (todo-67 rows dropped)
      execution-ledger.jsonl           complete execution ledger
      gates/<gate-dir>/gate-result.json   canonical phase Gate evidence
      h1/checkpoint-result.json        H1 operator checkpoint artifact
      h1/binding.json                  total H1 binding (this module)
      task-evidence/task-<N>-foundation-video-pipeline.json  (N = 1..66)
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from services.contracts.primitives import Sha256, StrictModel

GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$", strict=True)]

SOURCE_DIR: Final = "source"
INPUTS_DIR: Final = "inputs"
IDENTITY_NAME: Final = "identity.json"
GATES_INPUT_DIR: Final = "gates"
H1_INPUT_DIR: Final = "h1"
H1_CHECKPOINT_NAME: Final = "checkpoint-result.json"
H1_BINDING_NAME: Final = "binding.json"
TASK_EVIDENCE_INPUT_DIR: Final = "task-evidence"
PLAN_INPUT_NAME: Final = "plan.md"
START_WORK_LEDGER_INPUT: Final = "start-work-ledger.jsonl"
EXECUTION_LEDGER_INPUT: Final = "execution-ledger.jsonl"
TASK_EVIDENCE_COUNT: Final = 66
TODO_EXCLUSION_MARKER: Final = "todo-67"

GATE_DIRS: Final[dict[str, str]] = {
    "phase-0a": "phase-0a",
    "phase-0b": "phase-0b",
    "phase-0c": "phase-0c",
    "control-plane": "control-plane-baseline",
    "phase-1-technical": "phase-1-technical",
    "phase-2": "phase-2",
    "phase-3": "phase-3",
}

_TASK_EVIDENCE_RE: Final = re.compile(r"^task-(\d+)-foundation-video-pipeline\.json$")


class ReleaseIdentity(StrictModel):
    """Deterministic candidate identity (no wall-clock fields)."""

    schema_version: Literal["release-identity-v1"] = "release-identity-v1"
    git_sha: GitSha
    source_tree_sha256: Sha256
    plan_sha256: Sha256
    start_work_ledger_suffix_sha256: Sha256
    execution_ledger_sha256: Sha256
    produced_by: Literal["services.release.build_candidate"] = "services.release.build_candidate"


class H1BoundWorld(StrictModel):
    edit_source_world_sha256: Sha256
    final_plan_sha256: Sha256
    final_ir_sha256: Sha256
    final_preview_sha256: Sha256


class H1TotalBinding(StrictModel):
    """Total binding of the H1 operator checkpoint into the candidate."""

    schema_version: Literal["h1-binding-v1"] = "h1-binding-v1"
    purpose: Literal["operator_checkpoint"]
    decision: Literal["EDITORIAL_APPROVED"]
    checkpoint_sha256: Sha256
    fixture_only: Literal[False]
    episode_id: str = Field(min_length=1)
    bound_world: H1BoundWorld
    git_sha: GitSha


class ModeBits(StrictModel):
    files_octal: str
    directories_octal: str
    writable_paths: tuple[str, ...]
    all_readonly: bool


class CandidateVerification(StrictModel):
    schema_version: Literal["candidate-verification-v1"] = "candidate-verification-v1"
    candidate_path: str
    candidate_id: Sha256
    manifest_sha256: Sha256
    git_sha: GitSha
    source_tree_sha256: Sha256
    entry_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    modes: ModeBits


def task_evidence_name(todo: int) -> str:
    if not 1 <= todo <= TASK_EVIDENCE_COUNT:
        raise ValueError(f"task evidence todo out of range: {todo}")
    return f"task-{todo}-foundation-video-pipeline.json"


def parse_task_evidence_number(name: str) -> int | None:
    match = _TASK_EVIDENCE_RE.match(name)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= TASK_EVIDENCE_COUNT else -1


__all__ = [
    "EXECUTION_LEDGER_INPUT",
    "GATES_INPUT_DIR",
    "GATE_DIRS",
    "H1_BINDING_NAME",
    "H1_CHECKPOINT_NAME",
    "H1_INPUT_DIR",
    "IDENTITY_NAME",
    "INPUTS_DIR",
    "PLAN_INPUT_NAME",
    "SOURCE_DIR",
    "START_WORK_LEDGER_INPUT",
    "TASK_EVIDENCE_COUNT",
    "TASK_EVIDENCE_INPUT_DIR",
    "TODO_EXCLUSION_MARKER",
    "CandidateVerification",
    "GitSha",
    "H1BoundWorld",
    "H1TotalBinding",
    "ModeBits",
    "ReleaseIdentity",
    "parse_task_evidence_number",
    "task_evidence_name",
]
