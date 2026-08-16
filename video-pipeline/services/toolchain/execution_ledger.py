from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.foundation_io import canonical_model_bytes

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExecutionLedgerError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class LedgerRow(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    sequence: int = Field(gt=0)
    event_type: str
    previous_event_hash: Sha256


class ToolchainBoundEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    sequence: int = Field(gt=1)
    previous_event_hash: Sha256
    event_type: Literal["toolchain-bound"] = "toolchain-bound"
    work_id: Sha256
    ffmpeg_bin: str
    ffmpeg_sha256: Sha256
    ffprobe_bin: str
    ffprobe_sha256: Sha256


class ToolchainBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    work_id: Sha256
    ffmpeg_bin: str
    ffmpeg_sha256: Sha256
    ffprobe_bin: str
    ffprobe_sha256: Sha256

    @field_validator("ffmpeg_bin", "ffprobe_bin")
    @classmethod
    def require_absolute_binary_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("toolchain binary paths must be absolute")
        return value


def _validated_rows(raw: bytes) -> tuple[tuple[bytes, LedgerRow], ...]:
    lines = tuple(line for line in raw.splitlines() if line)
    if not lines:
        raise ExecutionLedgerError("execution ledger is empty")
    rows: list[tuple[bytes, LedgerRow]] = []
    for index, line in enumerate(lines):
        try:
            row = LedgerRow.model_validate_json(line)
        except ValidationError as error:
            detail = f"invalid execution ledger row {index + 1}: {error}"
            raise ExecutionLedgerError(detail) from error
        if line != canonical_model_bytes(row):
            raise ExecutionLedgerError(f"noncanonical execution ledger row {index + 1}")
        expected_sequence = index + 1
        expected_previous = "0" * 64 if index == 0 else hashlib.sha256(lines[index - 1]).hexdigest()
        if row.sequence != expected_sequence or row.previous_event_hash != expected_previous:
            raise ExecutionLedgerError(f"execution ledger chain drift at row {index + 1}")
        rows.append((line, row))
    return tuple(rows)


def append_toolchain_bound(ledger: Path, binding: ToolchainBinding) -> ToolchainBoundEvent:
    descriptor = os.open(ledger, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        raw = os.read(descriptor, os.fstat(descriptor).st_size)
        rows = _validated_rows(raw)
        for line, _row in rows:
            try:
                existing = ToolchainBoundEvent.model_validate_json(line)
            except ValidationError:
                continue
            if existing.work_id == binding.work_id:
                if (
                    existing.ffmpeg_bin != binding.ffmpeg_bin
                    or existing.ffmpeg_sha256 != binding.ffmpeg_sha256
                    or existing.ffprobe_bin != binding.ffprobe_bin
                    or existing.ffprobe_sha256 != binding.ffprobe_sha256
                ):
                    raise ExecutionLedgerError("conflicting toolchain-bound event")
                return existing
        head_raw, head = rows[-1]
        event = ToolchainBoundEvent(
            sequence=head.sequence + 1,
            previous_event_hash=hashlib.sha256(head_raw).hexdigest(),
            work_id=binding.work_id,
            ffmpeg_bin=binding.ffmpeg_bin,
            ffmpeg_sha256=binding.ffmpeg_sha256,
            ffprobe_bin=binding.ffprobe_bin,
            ffprobe_sha256=binding.ffprobe_sha256,
        )
        os.write(descriptor, canonical_model_bytes(event) + b"\n")
        os.fsync(descriptor)
        return event
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--ffmpeg-bin", required=True)
    parser.add_argument("--ffmpeg-sha256", required=True)
    parser.add_argument("--ffprobe-bin", required=True)
    parser.add_argument("--ffprobe-sha256", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        event = append_toolchain_bound(
            arguments.ledger,
            ToolchainBinding(
                work_id=arguments.work_id,
                ffmpeg_bin=arguments.ffmpeg_bin,
                ffmpeg_sha256=arguments.ffmpeg_sha256,
                ffprobe_bin=arguments.ffprobe_bin,
                ffprobe_sha256=arguments.ffprobe_sha256,
            ),
        )
    except (ExecutionLedgerError, OSError, ValidationError) as error:
        print(error)
        return 2
    print(f"toolchain-bound sequence={event.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
