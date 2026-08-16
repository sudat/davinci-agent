from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class WorkInitializationError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class LedgerHeader(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    event: str


class WorkInitialized(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event: Literal["work-initialized"]
    plan_path: Path
    initial_plan_sha256: Sha256
    execution_work_id: Sha256
    attempt_dir: Path
    plan_input: Path
    execution_ledger: Path
    uv_bin: Path
    uv_sha256: Sha256
    python_bin: Path
    python_sha256: Sha256
    created_at: str

    @field_validator(
        "plan_path",
        "attempt_dir",
        "plan_input",
        "execution_ledger",
        "uv_bin",
        "python_bin",
    )
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("work initialization paths must be absolute")
        return value


@dataclass(frozen=True, slots=True)
class ParsedLedgerRow:
    index: int
    raw: bytes
    header: LedgerHeader


class RestoreReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_work_id: Sha256
    attempt_dir: Path
    uv_bin: Path
    python_bin: Path


def _parse_rows(ledger: Path) -> tuple[ParsedLedgerRow, ...]:
    data = ledger.read_bytes()
    lines = data.splitlines(keepends=True)
    rows: list[ParsedLedgerRow] = []
    for index, line in enumerate(lines):
        raw = line.rstrip(b"\r\n")
        if not raw:
            continue
        try:
            header = LedgerHeader.model_validate_json(raw)
        except ValidationError as error:
            is_incomplete_tail = index == len(lines) - 1 and not line.endswith((b"\n", b"\r"))
            if is_incomplete_tail:
                continue
            raise WorkInitializationError(f"invalid ledger row {index + 1}: {error}") from error
        rows.append(ParsedLedgerRow(index=index, raw=raw, header=header))
    return tuple(rows)


def _active_records(rows: tuple[ParsedLedgerRow, ...]) -> tuple[WorkInitialized, ...]:
    initialized: list[tuple[int, WorkInitialized]] = []
    revoked: list[tuple[int, str]] = []
    for row in rows:
        if row.header.event == "work-initialized":
            try:
                initialized.append((row.index, WorkInitialized.model_validate_json(row.raw)))
            except ValidationError as error:
                raise WorkInitializationError(f"invalid work-initialized row: {error}") from error
        if row.header.event == "work-revoked":
            extra = row.header.model_extra or {}
            work_id = extra.get("execution_work_id")
            if not isinstance(work_id, str):
                raise WorkInitializationError("work-revoked row lacks execution_work_id")
            revoked.append((row.index, work_id))
    return tuple(
        record
        for index, record in initialized
        if not any(
            revoke_index > index and work_id == record.execution_work_id
            for revoke_index, work_id in revoked
        )
    )


def restore_by_plan_input(ledger: Path, plan_input: Path) -> WorkInitialized:
    matching = [
        record
        for record in _active_records(_parse_rows(ledger))
        if record.plan_input == plan_input.resolve()
    ]
    if len(matching) != 1:
        detail = f"expected one active work initialization, found {len(matching)}"
        raise WorkInitializationError(detail)
    record = matching[0]
    expected_id = hashlib.sha256(
        str(record.plan_path).encode() + b"\x00" + bytes.fromhex(record.initial_plan_sha256)
    ).hexdigest()
    if record.execution_work_id != expected_id:
        raise WorkInitializationError("execution work ID does not match plan identity")
    if sha256_file(record.plan_input) != record.initial_plan_sha256:
        raise WorkInitializationError("plan input hash drift")
    if sha256_file(record.uv_bin) != record.uv_sha256:
        raise WorkInitializationError("uv binary hash drift")
    if sha256_file(record.python_bin) != record.python_sha256:
        raise WorkInitializationError("Python binary hash drift")
    return record


def restore_for_plan(ledger: Path, plan_path: Path, _initial_plan_sha256: str) -> WorkInitialized:
    if not plan_path.is_absolute():
        raise WorkInitializationError("plan path must be absolute")
    matching = [
        record
        for record in _active_records(_parse_rows(ledger))
        if record.plan_path == plan_path
    ]
    if len(matching) != 1:
        detail = f"expected one active work initialization, found {len(matching)}"
        raise WorkInitializationError(detail)
    return restore_by_plan_input(ledger, matching[0].plan_input)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--plan-path", type=Path, required=True)
    parser.add_argument("--initial-plan-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--boulder", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        record = restore_for_plan(
            arguments.ledger,
            arguments.plan_path,
            arguments.initial_plan_sha256,
        )
        receipt = RestoreReceipt(
            execution_work_id=record.execution_work_id,
            attempt_dir=record.attempt_dir,
            uv_bin=record.uv_bin,
            python_bin=record.python_bin,
        )
        payload = canonical_model_bytes(receipt)
        atomic_write(arguments.out, payload)
        if arguments.boulder is not None:
            atomic_write(arguments.boulder, payload)
    except (OSError, WorkInitializationError) as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
