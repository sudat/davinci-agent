from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from services.contracts.primitives import Sha256, StrictModel


class ExitCodeAssertion(StrictModel):
    kind: Literal["exit_code"] = "exit_code"
    expected: int
    passed: bool


class StdoutContainsAssertion(StrictModel):
    kind: Literal["stdout_contains"] = "stdout_contains"
    expected: str
    passed: bool


class StderrContainsAssertion(StrictModel):
    kind: Literal["stderr_contains"] = "stderr_contains"
    expected: str
    passed: bool


GateAssertion = Annotated[
    ExitCodeAssertion | StdoutContainsAssertion | StderrContainsAssertion,
    Field(discriminator="kind"),
]


class FileHash(StrictModel):
    path: str
    sha256: Sha256


class CommandSpec(StrictModel):
    attempt_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: Path
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    assertions: tuple[GateAssertion, ...] = Field(min_length=1)


class AttemptEvent(StrictModel):
    event_type: Literal["attempt"] = "attempt"
    sequence: int = Field(gt=0)
    previous_event_hash: Sha256
    event_hash: Sha256
    attempt_id: str
    command: tuple[str, ...]
    cwd: str
    inputs: tuple[FileHash, ...]


class CompletionEvent(StrictModel):
    event_type: Literal["completion"] = "completion"
    sequence: int = Field(gt=0)
    previous_event_hash: Sha256
    event_hash: Sha256
    attempt_id: str
    command: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout_path: str
    stdout_sha256: Sha256
    stderr_path: str
    stderr_sha256: Sha256
    inputs: tuple[FileHash, ...]
    outputs: tuple[FileHash, ...]
    assertions: tuple[GateAssertion, ...]


EvidenceEvent = Annotated[AttemptEvent | CompletionEvent, Field(discriminator="event_type")]
EVENT_ADAPTER: TypeAdapter[EvidenceEvent] = TypeAdapter(EvidenceEvent)


class RetainedHead(StrictModel):
    sequence: int = Field(gt=0)
    event_hash: Sha256


class AssertionResult(StrictModel):
    attempt_id: str
    kind: Literal["exit_code", "stdout_contains", "stderr_contains"]
    authored_passed: bool
    recomputed_passed: bool


class VerificationReport(StrictModel):
    chain_valid: bool
    retained_head: Sha256
    recomputed_passed: bool
    assertions: tuple[AssertionResult, ...]
    incomplete_attempt_ids: tuple[str, ...]
    partial_tail_ignored: bool
    outside_guarantee: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
