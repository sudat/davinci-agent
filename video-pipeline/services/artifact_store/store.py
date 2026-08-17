"""Immutable content-addressed Artifact store with atomic publication.

Store layout: ``objects/<sha[:2]>/<sha256>`` objects, fixed-name in-flight
temps ``.obj-<sha>.tmp``, ``artifacts/<artifact_id>.json`` meta sidecars, and
``journal/<sha256>.json`` intents journaled before the temp. Every component
below ``store_root`` is validated: symlinks, escapes, and non-store names are
refused; reads re-verify content hashes from bytes (stale CAS suppression)."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

from services.artifact_store.models import (
    ContentAddressedRef,
    PublicationIntent,
    PublicationReceipt,
)
from services.foundation_io import atomic_write, canonical_model_bytes

OBJECTS_DIR = "objects"
ARTIFACTS_DIR = "artifacts"
JOURNAL_DIR = "journal"
_FORBIDDEN_COMPONENT_CHARS = ("/", "\\", "\0")


class StoreRefusalError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


_FORBIDDEN_COMPONENT_CHARS = ("/", "\\", "\0")


def _validate_component(component: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or any(char in component for char in _FORBIDDEN_COMPONENT_CHARS)
    ):
        raise StoreRefusalError("path-escape", f"refusing store path component: {component!r}")


def resolve_within(store_root: Path, parts: tuple[str, ...]) -> Path:
    """Resolve ``parts`` under ``store_root`` refusing symlinks and escapes."""

    root_resolved = store_root.resolve(strict=True)
    current = store_root
    for component in parts:
        _validate_component(component)
        candidate = current / component
        if candidate.is_symlink():
            raise StoreRefusalError(
                "symlinked-path-component",
                f"refusing symlinked store path component: {candidate}",
            )
        current = candidate
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise StoreRefusalError("path-escape", f"resolved store path escapes root: {resolved}")
    return resolved


def _dir_within(store_root: Path, parts: tuple[str, ...]) -> Path:
    resolved = resolve_within(store_root, parts)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _read_file_nofollow(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StoreRefusalError(
                "symlinked-path-component",
                f"refusing symlinked store file: {path}",
            ) from error
        raise
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable(path: Path, payload: bytes, drift_code: str) -> None:
    existing = _read_file_nofollow(path)
    if existing is not None and existing != payload:
        raise StoreRefusalError(drift_code, f"immutable store file differs: {path}")
    if existing is None:
        atomic_write(path, payload)


def _temp_name(content_sha256: str) -> str:
    return f".obj-{content_sha256}.tmp"


class ArtifactStore:
    def __init__(self, store_root: Path) -> None:
        self._root = store_root
        store_root.mkdir(parents=True, exist_ok=True)

    @property
    def store_root(self) -> Path:
        return self._root

    def journal_intent(self, intent: PublicationIntent) -> None:
        journal_dir = _dir_within(self._root, (JOURNAL_DIR,))
        path = journal_dir / f"{intent.envelope.content_hash}.json"
        _write_immutable(path, canonical_model_bytes(intent), "journal-drift")

    def write_object_temp(self, content_sha256: str, payload: bytes) -> Path:
        digest = hashlib.sha256(payload).hexdigest()
        if digest != content_sha256:
            raise StoreRefusalError(
                "content-hash-mismatch",
                "payload bytes do not hash to the declared content sha256",
            )
        shard_dir = _dir_within(self._root, (OBJECTS_DIR, content_sha256[:2]))
        temp_path = shard_dir / _temp_name(content_sha256)
        existing = _read_file_nofollow(temp_path)
        if existing is not None:
            if existing != payload:
                raise StoreRefusalError(
                    "temp-drift",
                    f"existing temp bytes differ for {content_sha256}",
                )
            return temp_path
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(shard_dir)
        return temp_path

    def rename_object(self, content_sha256: str) -> None:
        shard_dir = _dir_within(self._root, (OBJECTS_DIR, content_sha256[:2]))
        temp_path = shard_dir / _temp_name(content_sha256)
        final_path = shard_dir / content_sha256
        if final_path.is_symlink():
            raise StoreRefusalError(
                "symlinked-path-component",
                f"refusing symlinked object path: {final_path}",
            )
        if final_path.exists():
            temp_path.unlink(missing_ok=True)
            return
        if not temp_path.exists():
            raise StoreRefusalError(
                "temp-missing",
                f"no in-flight temp to rename for {content_sha256}",
            )
        temp_path.rename(final_path)
        _fsync_directory(shard_dir)

    def write_meta(self, intent: PublicationIntent) -> None:
        artifacts_dir = _dir_within(self._root, (ARTIFACTS_DIR,))
        path = artifacts_dir / f"{intent.envelope.artifact_id}.json"
        _write_immutable(path, canonical_model_bytes(intent), "meta-overwrite-refused")

    def _existing_object(self, content_sha256: str) -> bytes | None:
        shard_dir = _dir_within(self._root, (OBJECTS_DIR, content_sha256[:2]))
        return _read_file_nofollow(shard_dir / content_sha256)

    def publish(self, intent: PublicationIntent, payload: bytes) -> PublicationReceipt:
        content_sha256 = intent.envelope.content_hash
        digest = hashlib.sha256(payload).hexdigest()
        if digest != content_sha256:
            raise StoreRefusalError(
                "content-hash-mismatch",
                "payload bytes do not hash to the declared content sha256",
            )
        self._preflight_artifacts_dir()
        self.journal_intent(intent)
        existing = self._existing_object(content_sha256)
        idempotent = existing is not None
        if existing is None:
            self.write_object_temp(content_sha256, payload)
            self.rename_object(content_sha256)
        elif existing != payload:
            raise StoreRefusalError(
                "overwrite-refused",
                f"object {content_sha256} already exists with different bytes",
            )
        self.write_meta(intent)
        return PublicationReceipt(
            artifact_id=intent.envelope.artifact_id,
            content_sha256=content_sha256,
            object_ref=self._object_ref(content_sha256, len(payload)),
            meta_relative_path=f"{ARTIFACTS_DIR}/{intent.envelope.artifact_id}.json",
            idempotent=idempotent,
        )

    def _preflight_artifacts_dir(self) -> None:
        artifacts_dir = self._root / ARTIFACTS_DIR
        if artifacts_dir.is_symlink():
            raise StoreRefusalError(
                "symlinked-path-component",
                f"refusing symlinked store path component: {artifacts_dir}",
            )

    def _object_ref(self, content_sha256: str, size: int) -> ContentAddressedRef:
        return ContentAddressedRef(
            store_root=str(self._root),
            shard=content_sha256[:2],
            sha256=content_sha256,
            size=size,
        )

    def reopen(self, content_sha256: str) -> tuple[bytes, ContentAddressedRef]:
        existing = self._existing_object(content_sha256)
        if existing is None:
            raise StoreRefusalError(
                "object-missing",
                f"no object stored for {content_sha256}",
            )
        digest = hashlib.sha256(existing).hexdigest()
        if digest != content_sha256:
            raise StoreRefusalError(
                "content-hash-mismatch",
                f"stored object content hash drift: {digest} != {content_sha256}",
            )
        return existing, self._object_ref(content_sha256, len(existing))
