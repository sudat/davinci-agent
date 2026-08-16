from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

EXCLUDED_NAMES = frozenset(
    {
        ".DS_Store",
        ".git",
        ".mypy_cache",
        ".omo",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "generated-media",
        "jobs",
        "private",
        "venv",
    }
)
INCLUDED_DIRS = frozenset(
    {"assets", "build", "config", "runbooks", "schemas", "services", "templates", "tests"}
)
INCLUDED_ROOT_FILES = frozenset({".python-version", "pyproject.toml", "uv.lock"})
INCLUDED_SUFFIXES = frozenset(
    {".json", ".jsonl", ".lock", ".md", ".py", ".pyi", ".toml", ".txt", ".yaml", ".yml"}
)


class SnapshotError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class SnapshotEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    size: int
    sha256: str


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["source-snapshot-v1"] = "source-snapshot-v1"
    execution_contract_sha256: str
    file_count: int
    tree_sha256: str
    entries: tuple[SnapshotEntry, ...]


def _included(relative: Path) -> bool:
    if relative.name in INCLUDED_ROOT_FILES and len(relative.parts) == 1:
        return True
    if not relative.parts or relative.parts[0] not in INCLUDED_DIRS:
        return False
    if relative.parts[0] in {"assets", "templates"}:
        return True
    return relative.suffix.lower() in INCLUDED_SUFFIXES or relative.name == ".gitkeep"


def _walk(root: Path, directory: Path) -> list[SnapshotEntry]:
    entries: list[SnapshotEntry] = []
    with os.scandir(directory) as iterator:
        for item in sorted(iterator, key=lambda candidate: candidate.name):
            if item.name in EXCLUDED_NAMES:
                continue
            path = Path(item.path)
            relative = path.relative_to(root)
            if item.is_symlink():
                raise SnapshotError(f"symlink rejected: {relative.as_posix()}")
            if item.is_dir(follow_symlinks=False):
                if directory == root and item.name not in INCLUDED_DIRS:
                    continue
                entries.extend(_walk(root, path))
                continue
            metadata = item.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or not _included(relative):
                continue
            entries.append(
                SnapshotEntry(
                    path=relative.as_posix(),
                    size=metadata.st_size,
                    sha256=sha256_file(path),
                )
            )
    return entries


def create_snapshot(root: Path, execution_contract: Path, out: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise SnapshotError("snapshot root is not a directory")
        entries = tuple(sorted(_walk(resolved_root, resolved_root), key=lambda entry: entry.path))
        tree = hashlib.sha256()
        for entry in entries:
            tree.update(f"{entry.path}\x00{entry.sha256}\n".encode())
        snapshot = SourceSnapshot(
            execution_contract_sha256=sha256_file(execution_contract.resolve(strict=True)),
            file_count=len(entries),
            tree_sha256=tree.hexdigest(),
            entries=entries,
        )
        atomic_write(out, canonical_model_bytes(snapshot))
    except OSError as error:
        raise SnapshotError(str(error)) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execution-contract", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        create_snapshot(arguments.root, arguments.execution_contract, arguments.out)
    except SnapshotError as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
