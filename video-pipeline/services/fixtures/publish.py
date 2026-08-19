from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from services.evidence.append_event import (
    append_freeze_event,
    require_intent_recorded,
)
from services.fixtures.models import (
    ControlPlaneFreezeReceipt,
    FreezeIntent,
    FreezeReceipt,
    Phase0BFreezeReceipt,
    Phase0CFreezeReceipt,
    Phase1TechnicalFreezeReceipt,
    Phase2FreezeReceipt,
)
from services.foundation_io import canonical_model_bytes
from services.gates import GatePolicy, canonical_gate_bytes
from services.toolchain.execution_ledger import ExecutionLedgerError

type AnyFreezeReceipt = (
    FreezeReceipt
    | Phase0BFreezeReceipt
    | Phase0CFreezeReceipt
    | ControlPlaneFreezeReceipt
    | Phase1TechnicalFreezeReceipt
    | Phase2FreezeReceipt
)

AT_FDCWD = -2
RENAME_EXCL = 0x00000004


@dataclass(frozen=True, slots=True)
class PublishError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_fixed_temp(source: Path, temporary: Path, expected: bytes) -> None:
    if source.read_bytes() != expected:
        raise PublishError(f"staged bytes drift: {source}")
    if temporary.exists():
        if temporary.read_bytes() != expected:
            raise PublishError(f"fixed temporary bytes drift: {temporary}")
        return
    temporary.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, expected)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(temporary.parent)


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(error_number, os.strerror(error_number), destination)
    _fsync_directory(destination.parent)


def _publish_one(source: Path, destination: Path, expected: bytes) -> None:
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != expected:
            raise PublishError(f"frozen final bytes drift: {destination}")
        return
    temporary = destination.parent / f".{destination.name}.freeze-tmp"
    _stage_fixed_temp(source, temporary, expected)
    _rename_noreplace(temporary, destination)


def _load_receipt(raw: bytes) -> AnyFreezeReceipt:
    try:
        return FreezeReceipt.model_validate_json(raw)
    except ValidationError:
        try:
            return Phase0BFreezeReceipt.model_validate_json(raw)
        except ValidationError:
            try:
                return Phase0CFreezeReceipt.model_validate_json(raw)
            except ValidationError:
                try:
                    return ControlPlaneFreezeReceipt.model_validate_json(raw)
                except ValidationError:
                    try:
                        return Phase1TechnicalFreezeReceipt.model_validate_json(raw)
                    except ValidationError:
                        return Phase2FreezeReceipt.model_validate_json(raw)


def publish_freeze(intent_path: Path, ledger: Path, *, recover: bool) -> None:
    if not recover:
        raise PublishError("publish requires --recover")
    intent_raw = intent_path.read_bytes()
    intent = FreezeIntent.model_validate_json(intent_raw)
    if intent_raw != canonical_model_bytes(intent):
        raise PublishError("noncanonical freeze intent")
    require_intent_recorded(ledger, intent)
    policy_source = Path(intent.staged_policy_path)
    receipt_source = Path(intent.staged_receipt_path)
    policy_bytes = policy_source.read_bytes()
    policy = GatePolicy.model_validate_json(policy_bytes)
    if policy_bytes != canonical_gate_bytes(policy):
        raise PublishError("noncanonical prepared policy")
    if policy.gate_id != intent.gate_id:
        raise PublishError("prepared policy gate differs from freeze intent")
    receipt_bytes = receipt_source.read_bytes()
    receipt = _load_receipt(receipt_bytes)
    if receipt_bytes != canonical_model_bytes(receipt):
        raise PublishError("noncanonical prepared receipt")
    if receipt.gate_id != intent.gate_id:
        raise PublishError("prepared receipt gate differs from freeze intent")
    if hashlib.sha256(policy_bytes).hexdigest() != intent.policy_sha256:
        raise PublishError("prepared policy hash drift")
    if hashlib.sha256(receipt_bytes).hexdigest() != intent.receipt_sha256:
        raise PublishError("prepared receipt hash drift")
    policy_path = Path(intent.policy_path)
    receipt_path = Path(intent.receipt_path)
    if receipt_path.exists() and not policy_path.exists():
        raise PublishError("receipt-only recovery state is invalid")
    _publish_one(policy_source, policy_path, policy_bytes)
    _publish_one(receipt_source, receipt_path, receipt_bytes)
    append_freeze_event(ledger, intent, "freeze-completed")


def publish_errors() -> tuple[type[Exception], ...]:
    return (ExecutionLedgerError, OSError, PublishError, ValidationError)
