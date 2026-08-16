"""Hash-chain storage for local product Spike Evidence.

The chain detects mutation, reordering, and interior deletion relative to the
retained ``head.json``. It cannot detect executions that were never recorded,
replacement of both the full ledger and retained head, or rewrites by the file
owner/root. Those are explicit trust boundaries, not claimed detections.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.contracts import canonical_json_bytes
from services.evidence.models import (
    AttemptEvent,
    CommandSpec,
    CompletionEvent,
    EvidenceEvent,
    FileHash,
    RetainedHead,
)
from services.foundation_io import atomic_write, sha256_file

GENESIS_HASH: Final = "0" * 64


class EvidenceLedgerError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def hash_event(event: EvidenceEvent) -> str:
    payload = event.model_copy(update={"event_hash": GENESIS_HASH})
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _file_hashes(paths: tuple[Path, ...]) -> tuple[FileHash, ...]:
    return tuple(
        FileHash(path=str(path.resolve(strict=True)), sha256=sha256_file(path.resolve(strict=True)))
        for path in paths
    )


def _current_head(bundle: Path) -> RetainedHead | None:
    head_path = bundle / "head.json"
    if not head_path.exists():
        return None
    try:
        return RetainedHead.model_validate_json(head_path.read_bytes())
    except ValidationError as error:
        raise EvidenceLedgerError(f"invalid retained head: {error}") from error


def _prepare_ledger(bundle: Path, head: RetainedHead | None) -> Path:
    ledger = bundle / "ledger.jsonl"
    bundle.mkdir(parents=True, exist_ok=True)
    if not ledger.exists():
        if head is not None:
            raise EvidenceLedgerError("retained head exists without ledger")
        return ledger
    lines = ledger.read_bytes().splitlines(keepends=True)
    retained = 0 if head is None else head.sequence
    if len(lines) < retained:
        raise EvidenceLedgerError("ledger is shorter than retained head")
    suffix = lines[retained:]
    if suffix:
        if len(suffix) != 1 or suffix[0].endswith((b"\n", b"\r")):
            raise EvidenceLedgerError("unretained complete ledger row blocks append")
        with ledger.open("r+b") as stream:
            stream.truncate(sum(len(line) for line in lines[:retained]))
            stream.flush()
            os.fsync(stream.fileno())
    return ledger


def _append(bundle: Path, event: EvidenceEvent) -> None:
    head = _current_head(bundle)
    ledger = _prepare_ledger(bundle, head)
    with ledger.open("ab") as stream:
        stream.write(canonical_json_bytes(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    atomic_write(
        bundle / "head.json",
        canonical_json_bytes(RetainedHead(sequence=event.sequence, event_hash=event.event_hash)),
    )


def append_attempt(bundle: Path, specification: CommandSpec) -> AttemptEvent:
    head = _current_head(bundle)
    event = AttemptEvent(
        sequence=1 if head is None else head.sequence + 1,
        previous_event_hash=GENESIS_HASH if head is None else head.event_hash,
        event_hash=GENESIS_HASH,
        attempt_id=specification.attempt_id,
        command=specification.argv,
        cwd=str(specification.cwd.resolve(strict=True)),
        inputs=_file_hashes(specification.inputs),
    )
    retained = event.model_copy(update={"event_hash": hash_event(event)})
    _append(bundle, retained)
    return retained


def append_completion(
    bundle: Path,
    specification: CommandSpec,
    exit_code: int,
    stdout_path: Path,
    stderr_path: Path,
) -> CompletionEvent:
    head = _current_head(bundle)
    if head is None:
        raise EvidenceLedgerError("completion requires a retained attempt")
    event = CompletionEvent(
        sequence=head.sequence + 1,
        previous_event_hash=head.event_hash,
        event_hash=GENESIS_HASH,
        attempt_id=specification.attempt_id,
        command=specification.argv,
        cwd=str(specification.cwd.resolve(strict=True)),
        exit_code=exit_code,
        stdout_path=str(stdout_path.resolve(strict=True)),
        stdout_sha256=sha256_file(stdout_path),
        stderr_path=str(stderr_path.resolve(strict=True)),
        stderr_sha256=sha256_file(stderr_path),
        inputs=_file_hashes(specification.inputs),
        outputs=_file_hashes(specification.outputs),
        assertions=specification.assertions,
    )
    retained = event.model_copy(update={"event_hash": hash_event(event)})
    _append(bundle, retained)
    return retained
