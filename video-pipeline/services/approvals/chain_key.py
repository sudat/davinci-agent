"""Local HMAC key that seals the operation-record chain (Todo 66 fix).

``record_hash`` is HMAC-SHA256 over the canonical record bytes (hash field
zeroed) keyed by a local secret, so an offline rewrite that recomputes a
self-consistent unkeyed chain no longer verifies: the forger needs the
key file. The key is 32 random bytes, hex-encoded, stored next to the
records file as ``<records>.hmac-key`` with mode 0600, and is created
only by the append path; read paths never create it (a missing key with
existing records fails closed as ``chain-key-missing``).

``OPERATION_RECORD_CHAIN_KEY`` (hex) overrides the file — an explicit
TEST SEAM, not a production path.

Honest scope: the key is local disc state readable by the same user (and
root). This raises the bar from "recompute sha256 offline" to "read the
operator's key file"; it is tamper evidence against offline rewrites,
NOT a trust boundary against the local user or root — the boundary
remains the machine itself, exactly as the PRD's single-user H1 model
assumes.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Final

CHAIN_KEY_ENV: Final = "OPERATION_RECORD_CHAIN_KEY"
CHAIN_KEY_FILE_SUFFIX: Final = ".hmac-key"
CHAIN_KEY_BYTES: Final = 32
CHAIN_KEY_FILE_MODE: Final = 0o600


class ChainKeyError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def key_path_for(records_path: Path) -> Path:
    return records_path.with_name(records_path.name + CHAIN_KEY_FILE_SUFFIX)


def _from_hex(value: str, *, source: str) -> bytes:
    try:
        key = bytes.fromhex(value)
    except ValueError as error:
        raise ChainKeyError(
            "chain-key-invalid", f"{source} is not hex: {error}"
        ) from error
    if len(key) < CHAIN_KEY_BYTES:
        raise ChainKeyError(
            "chain-key-invalid",
            f"{source} must be at least {CHAIN_KEY_BYTES} bytes of hex",
        )
    return key


def _read_key_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        raw = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    return _from_hex(raw.decode("ascii").strip(), source=f"key file {path}")


def _create_key_file(path: Path) -> bytes:
    key = secrets.token_bytes(CHAIN_KEY_BYTES)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, CHAIN_KEY_FILE_MODE
    )
    try:
        os.write(descriptor, key.hex().encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return key


def load_chain_key(records_path: Path, *, create: bool) -> bytes:
    """Resolve the chain key: env override, else the key file.

    ``create=True`` (append path) generates and persists the key on first
    use; ``create=False`` (verify paths) never creates and raises
    ``chain-key-missing`` when records exist but no key can be resolved.
    """

    override = os.environ.get(CHAIN_KEY_ENV)
    if override:
        return _from_hex(override, source=f"env {CHAIN_KEY_ENV}")
    path = key_path_for(records_path)
    if path.exists():
        return _read_key_file(path)
    if not create:
        raise ChainKeyError(
            "chain-key-missing",
            f"operation records exist at {records_path} but the sealing key "
            f"{path} is absent (deleted or forged store); refusing to verify",
        )
    try:
        return _create_key_file(path)
    except FileExistsError:
        return _read_key_file(path)


__all__ = [
    "CHAIN_KEY_BYTES",
    "CHAIN_KEY_ENV",
    "CHAIN_KEY_FILE_MODE",
    "CHAIN_KEY_FILE_SUFFIX",
    "ChainKeyError",
    "key_path_for",
    "load_chain_key",
]
