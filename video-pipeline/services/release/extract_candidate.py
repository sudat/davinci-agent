"""Extract a byte-faithful copy of a verified release candidate.

Lane contract: ``--candidate``, ``--no-follow`` (mandatory; extraction is
always symlink-free), ``--source-only`` (only ``source/``), ``--out``.
The candidate is verified (manifest recompute) before anything is copied,
and the extract receipt records candidate identity for lane comparison.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Literal

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.release.errors import ReleaseGateError
from services.release.manifest import tree_hash
from services.release.models import SOURCE_DIR
from services.release.verify import verify_candidate


class ExtractReceipt(StrictModel):
    schema_version: Literal["release-extract-receipt-v1"] = "release-extract-receipt-v1"
    candidate_path: str
    candidate_id: Sha256
    source_tree_sha256: Sha256
    copied_files: int
    source_only: bool


def copy_tree(source: Path, destination: Path) -> int:
    """No-follow copy of regular files/directories; returns file count."""

    copied = 0
    stack: list[tuple[Path, Path]] = [(source, destination)]
    while stack:
        current, target = stack.pop()
        metadata = current.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
            stack.extend(
                (Path(entry.path), target / entry.name)
                for entry in sorted(os.scandir(current), key=lambda item: item.name)
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseGateError("stale_hash", f"refusing non-regular candidate entry: {current}")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = current.read_bytes()
        target.write_bytes(payload)
        target.chmod(0o644)
        copied += 1
    return copied


def extract_candidate(candidate: Path, out: Path, *, source_only: bool) -> ExtractReceipt:
    verification = verify_candidate(
        candidate,
        expected_git_sha=None,
        require_h1_binding=False,
        recompute=True,
        require_readonly=False,
    )
    destination = out
    if source_only:
        source_root = candidate / SOURCE_DIR
        tree_root = destination
    else:
        source_root = candidate
        tree_root = destination / SOURCE_DIR
    out.mkdir(parents=True, exist_ok=True)
    copied = copy_tree(source_root, destination)
    receipt = ExtractReceipt(
        candidate_path=str(candidate),
        candidate_id=verification.candidate_id,
        source_tree_sha256=verification.source_tree_sha256
        if not source_only
        else tree_hash(tree_root),
        copied_files=copied,
        source_only=source_only,
    )
    atomic_write(out / "extract-receipt.json", canonical_model_bytes(receipt))
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--no-follow", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.no_follow:
        print("extraction is always symlink-free; pass --no-follow", file=sys.stderr)
        return 2
    try:
        receipt = extract_candidate(
            arguments.candidate,
            arguments.out,
            source_only=arguments.source_only,
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(canonical_model_bytes(receipt).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExtractReceipt",
    "copy_tree",
    "extract_candidate",
    "main",
]
