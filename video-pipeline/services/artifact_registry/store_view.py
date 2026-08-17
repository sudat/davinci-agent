"""Read-only filesystem access into the artifact store.

The registry never writes store bytes; these helpers only locate and read
content-addressed objects and meta sidecars with symlink refusal, so both
the registry index and reconciliation always re-derive truth from the
store instead of trusting index claims."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

from pydantic import ValidationError

from services.artifact_store.models import PublicationIntent

OBJECTS_DIR_NAME = "objects"
ARTIFACTS_DIR_NAME = "artifacts"
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


class StoreViewError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def object_path(store_root: Path, content_sha256: str) -> Path:
    return store_root / OBJECTS_DIR_NAME / content_sha256[:2] / content_sha256


def meta_path(store_root: Path, artifact_id: str) -> Path:
    return store_root / ARTIFACTS_DIR_NAME / f"{artifact_id}.json"


def read_file_nofollow(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StoreViewError(
                "symlinked-path",
                f"refusing symlinked registry file: {path}",
            ) from error
        raise
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def object_stats(path: Path) -> tuple[int, str] | None:
    raw = read_file_nofollow(path)
    if raw is None:
        return None
    return len(raw), hashlib.sha256(raw).hexdigest()


def parse_meta_bytes(raw: bytes, *, expected_id: str) -> PublicationIntent:
    try:
        intent = PublicationIntent.model_validate_json(raw)
    except ValidationError as error:
        raise StoreViewError("meta-invalid", f"store meta is invalid: {error}") from error
    if intent.envelope.artifact_id != expected_id:
        raise StoreViewError(
            "meta-mismatch",
            f"store meta artifact id {intent.envelope.artifact_id} != {expected_id}",
        )
    return intent


def is_object_name(name: str) -> bool:
    return len(name) == _SHA256_HEX_LENGTH and all(char in _HEX_DIGITS for char in name)


__all__ = [
    "ARTIFACTS_DIR_NAME",
    "OBJECTS_DIR_NAME",
    "StoreViewError",
    "is_object_name",
    "meta_path",
    "object_path",
    "object_stats",
    "parse_meta_bytes",
    "read_file_nofollow",
]
