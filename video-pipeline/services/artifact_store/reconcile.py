"""Deterministic crash reconciliation for in-flight publication temps.

Scans ``objects/<shard>/.obj-<sha>.tmp`` leftovers and classifies each one from
filesystem state alone:

- final object present  -> ``discard-stale-temp`` (rename completed; temp junk)
- journal intent exists -> ``discard-crashed-temp`` (crash before rename; the
  publication is resumable idempotently because the journal proves the intent)
- otherwise             -> ``discard-orphan-temp`` (no journaled intent; foreign)

Every run appends one canonical report line to ``reconcile-log.jsonl``."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.foundation_io import canonical_model_bytes

TEMP_PATTERN = re.compile(r"^\.obj-(?P<sha>[0-9a-f]{64})\.tmp$")
RECONCILE_LOG_NAME = "reconcile-log.jsonl"

ReconcileActionName = Literal[
    "discard-crashed-temp",
    "discard-orphan-temp",
    "discard-stale-temp",
]


class ReconcileEntry(StrictModel):
    temp_relative_path: str
    journal_present: bool
    final_present: bool
    action: ReconcileActionName


class ReconcileReport(StrictModel):
    entries: tuple[ReconcileEntry, ...] = ()
    discarded_count: int = Field(ge=0, strict=True)


def _discard(store_root: Path, shard_dir: Path, temp_file: Path) -> ReconcileEntry:
    match = TEMP_PATTERN.match(temp_file.name)
    if match is None:
        raise ValueError(f"non-conventional temp discovered: {temp_file}")
    content_sha256: str = match.group("sha")
    final_present = (shard_dir / content_sha256).exists()
    journal_present = (store_root / "journal" / f"{content_sha256}.json").exists()
    if final_present:
        action: ReconcileActionName = "discard-stale-temp"
    elif journal_present:
        action = "discard-crashed-temp"
    else:
        action = "discard-orphan-temp"
    temp_file.unlink()
    return ReconcileEntry(
        temp_relative_path=temp_file.relative_to(store_root).as_posix(),
        journal_present=journal_present,
        final_present=final_present,
        action=action,
    )


def reconcile(store_root: Path) -> ReconcileReport:
    objects_dir = store_root / "objects"
    entries: list[ReconcileEntry] = []
    if objects_dir.is_dir():
        for shard_dir in sorted(path for path in objects_dir.iterdir() if path.is_dir()):
            temps = sorted(
                path for path in shard_dir.iterdir() if TEMP_PATTERN.match(path.name) is not None
            )
            entries.extend(_discard(store_root, shard_dir, temp) for temp in temps)
    report = ReconcileReport(entries=tuple(entries), discarded_count=len(entries))
    log_path = store_root / RECONCILE_LOG_NAME
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, canonical_model_bytes(report) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return report


__all__ = [
    "RECONCILE_LOG_NAME",
    "ReconcileActionName",
    "ReconcileEntry",
    "ReconcileReport",
    "reconcile",
]
