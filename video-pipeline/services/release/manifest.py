"""Canonical ``manifest-v1``: the release candidate's content manifest.

Compact UTF-8 JSON containing ONLY ``schema_version`` and path-sorted
``entries:[{"path","size","sha256"}]``. ``manifest.json`` is excluded from
entries; the candidate ID is the SHA-256 of the manifest bytes themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.foundation_io import sha256_file

MANIFEST_SCHEMA: str = "manifest-v1"
MANIFEST_NAME: str = "manifest.json"
_HEX = frozenset("0123456789abcdef")


class ManifestError(Exception):
    """Raised when a tree cannot be represented as ``manifest-v1``."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def require_canonical_relative_path(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value:
            raise PydanticCustomError(
                "path_not_nfc", "entry path must be NFC UTF-8: {path}", {"path": value}
            )
        if value.startswith("/") or "\\" in value or "\x00" in value:
            raise PydanticCustomError(
                "path_unsafe", "entry path must be slash-relative without NUL/backslash"
            )
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise PydanticCustomError(
                "path_unsafe", "entry path must not contain empty, '.', or '..' segments"
            )
        return value

    @field_validator("sha256")
    @classmethod
    def require_lower_hex(cls, value: str) -> str:
        if not set(value) <= _HEX:
            raise PydanticCustomError("hash_not_lower_hex", "sha256 must be lowercase hex")
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(default=MANIFEST_SCHEMA)
    entries: tuple[ManifestEntry, ...]

    @field_validator("schema_version")
    @classmethod
    def require_schema(cls, value: str) -> str:
        if value != MANIFEST_SCHEMA:
            raise PydanticCustomError(
                "schema_mismatch", "only manifest-v1 is accepted: {value}", {"value": value}
            )
        return value

    @model_validator(mode="after")
    def require_sorted_unique_paths(self) -> Manifest:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths):
            raise PydanticCustomError("entries_unsorted", "entries must be sorted by path")
        if len(set(paths)) != len(paths):
            raise PydanticCustomError("duplicate_path", "entries must have unique paths")
        if MANIFEST_NAME in paths:
            raise PydanticCustomError(
                "manifest_self_entry", "manifest.json must be excluded from entries"
            )
        folds: dict[str, str] = {}
        for path in paths:
            key = unicodedata.normalize("NFD", path).casefold()
            if key in folds:
                raise PydanticCustomError(
                    "case_fold_collision",
                    "paths collide under case folding: {a} vs {b}",
                    {"a": folds[key], "b": path},
                )
            folds[key] = path
        return self


def manifest_bytes(manifest: Manifest) -> bytes:
    """Deterministic compact UTF-8 JSON (sorted keys, no extra fields)."""

    payload = {
        "entries": [
            {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
            for entry in manifest.entries
        ],
        "schema_version": manifest.schema_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def candidate_id(manifest: Manifest) -> str:
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def _fold_key(path: str) -> str:
    return unicodedata.normalize("NFD", path).casefold()


def collect_entries(root: Path) -> tuple[ManifestEntry, ...]:
    """Walk ``root`` no-follow; reject symlinks/non-regular files and fold collisions."""

    seen_folds: dict[str, str] = {}
    collected: list[ManifestEntry] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            for item in sorted(iterator, key=lambda candidate: candidate.name):
                path = Path(item.path)
                if item.is_symlink():
                    raise ManifestError(f"symlink rejected: {path}")
                metadata = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    visit(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ManifestError(f"non-regular file rejected: {path}")
                relative = path.relative_to(root).as_posix()
                if unicodedata.normalize("NFC", relative) != relative:
                    raise ManifestError(f"path is not NFC UTF-8: {relative}")
                if relative == MANIFEST_NAME and directory == root:
                    continue
                fold = _fold_key(relative)
                if fold in seen_folds:
                    raise ManifestError(
                        f"case-fold collision: {seen_folds[fold]} vs {relative}"
                    )
                seen_folds[fold] = relative
                collected.append(
                    ManifestEntry(path=relative, size=metadata.st_size, sha256=sha256_file(path))
                )

    visit(root)
    return tuple(sorted(collected, key=lambda entry: entry.path))


def build_manifest(root: Path) -> Manifest:
    try:
        entries = collect_entries(root)
    except OSError as error:
        raise ManifestError(str(error)) from error
    return Manifest(entries=entries)


def recompute_manifest_bytes(root: Path) -> bytes:
    return manifest_bytes(build_manifest(root))


def tree_hash(root: Path) -> str:
    """Snapshot-style tree hash over ``root`` (sorted ``path\\0sha256\\n`` lines)."""

    digest = hashlib.sha256()
    for entry in collect_entries(root):
        digest.update(f"{entry.path}\x00{entry.sha256}\n".encode())
    return digest.hexdigest()


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "Manifest",
    "ManifestEntry",
    "ManifestError",
    "build_manifest",
    "candidate_id",
    "collect_entries",
    "manifest_bytes",
    "recompute_manifest_bytes",
    "tree_hash",
]
