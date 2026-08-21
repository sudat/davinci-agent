"""Build the immutable release candidate: stage, seal, publish (Todo 67).

``--phase stage`` prepares ``<out>.staging`` under an exclusive OS flock
(single writer; a crashed writer's leftover lock file is safely retaken
because the kernel released its flock). ``--phase seal`` re-acquires the
same flock, writes ``manifest-v1``, publishes with fsync + atomic rename,
chmods the whole candidate read-only, and recomputes the manifest
byte-identically before writing the build receipt.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import sys
from pathlib import Path
from typing import Literal

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.release.errors import ReleaseGateError
from services.release.manifest import MANIFEST_NAME, build_manifest, manifest_bytes
from services.release.models import CandidateVerification  # noqa: TC001 (pydantic runtime)
from services.release.staging import DEFAULT_ALLOW_UNTRACKED, ReleaseStaging, stage_release
from services.release.verify import verify_candidate

LOCK_SUFFIX = ".lock"
STAGING_SUFFIX = ".staging"


class BuildReceipt(StrictModel):
    """Seal-phase receipt written outside the candidate."""

    schema_version: Literal["release-build-receipt-v1"] = "release-build-receipt-v1"
    verification: CandidateVerification
    staging_reused: bool


def lock_path(out: Path) -> Path:
    return out.with_name(out.name + LOCK_SUFFIX)


def staging_path(out: Path) -> Path:
    return out.with_name(out.name + STAGING_SUFFIX)


class WriterLock:
    """Exclusive OS flock over the staging lock file.

    ``acquire`` refuses when the final candidate already exists, opens the
    lock file (optionally creating it for the stage phase), and takes a
    non-blocking exclusive flock: a LIVE second writer always fails with
    ``duplicate_writer``, while a leftover file from a crashed writer is
    retaken safely (the kernel released its flock at process death). The
    lock file is never unlinked by its mere presence — only ``release``
    removes it after a successful seal.
    """

    def __init__(self, out: Path, *, create: bool = True) -> None:
        self._out = out
        self._create = create
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._out.exists():
            raise ReleaseGateError("duplicate_writer", f"candidate already exists: {self._out}")
        lock = lock_path(self._out)
        if not self._create and not lock.exists():
            raise ReleaseGateError(
                "duplicate_writer", "seal requires the stage-phase writer lock"
            )
        lock.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | (os.O_CREAT if self._create else 0)
        descriptor = os.open(lock, flags, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ReleaseGateError(
                    "duplicate_writer", f"another writer holds {lock}"
                ) from error
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        """Full release: remove the lock file and close the flock."""
        if self._descriptor is None:
            return
        lock_path(self._out).unlink(missing_ok=True)
        os.close(self._descriptor)
        self._descriptor = None

    def abandon(self) -> None:
        """Close the flock but KEEP the file (stage→seal hand-off marker)."""
        if self._descriptor is None:
            return
        os.close(self._descriptor)
        self._descriptor = None


def acquire_writer_lock(out: Path, *, restart: bool) -> WriterLock:
    """Backward-compatible helper: take the exclusive writer flock."""
    del restart  # a leftover lock is retaken by flock semantics, never by unlink
    lock = WriterLock(out)
    lock.acquire()
    return lock


def release_writer_lock(lock: WriterLock) -> None:
    lock.release()


def _fsync_tree(root: Path) -> None:
    for directory, _, files in os.walk(root):
        for name in files:
            descriptor = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory, _, _ in os.walk(root, topdown=False):
        descriptor = os.open(Path(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def chmod_readonly(root: Path) -> None:
    """Files become 0444 and directories 0555 (deepest first)."""

    directories: list[Path] = []
    for directory, subdirectories, files in os.walk(root):
        directories.append(Path(directory))
        for name in files:
            (Path(directory) / name).chmod(0o444)
        for name in subdirectories:
            target = Path(directory) / name
            if target.is_symlink():
                raise ReleaseGateError("writable_candidate", f"symlink inside candidate: {target}")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        directory.chmod(0o555)


def seal_candidate(out: Path, receipt_out: Path, expected_git_sha: str | None) -> BuildReceipt:
    """Manifest, atomic publish, chmod read-only, recompute, verify."""

    staging = staging_path(out)
    if not staging.is_dir():
        raise ReleaseGateError("missing_release_input", f"missing staging tree: {staging}")
    manifest_payload = manifest_bytes(build_manifest(staging))
    atomic_write(staging / MANIFEST_NAME, manifest_payload)
    _fsync_tree(staging)
    staging.rename(out)
    parent = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    chmod_readonly(out)
    recomputed = manifest_bytes(build_manifest(out))
    if recomputed != manifest_payload:
        raise ReleaseGateError("stale_hash", "post-chmod manifest recomputation differs")
    verification = verify_candidate(
        out,
        expected_git_sha=expected_git_sha,
        require_h1_binding=True,
        recompute=True,
        require_readonly=True,
    )
    receipt = BuildReceipt(verification=verification, staging_reused=False)
    atomic_write(receipt_out, canonical_model_bytes(receipt))
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--start-work-ledger", type=Path, required=True)
    parser.add_argument("--execution-ledger", type=Path, required=True)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--phase", choices=("stage", "seal", "all"), default="all")
    parser.add_argument("--allow-untracked", action="append", default=list(DEFAULT_ALLOW_UNTRACKED))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    out = arguments.out
    lock: WriterLock | None = None
    try:
        if arguments.phase in ("stage", "all"):
            lock = WriterLock(out)
            lock.acquire()
            inputs = ReleaseStaging(
                repo=arguments.repo,
                git_sha=arguments.git_sha,
                plan=arguments.plan,
                start_work_ledger=arguments.start_work_ledger,
                execution_ledger=arguments.execution_ledger,
                attempt_dir=arguments.attempt_dir,
                allow_untracked=tuple(arguments.allow_untracked),
            )
            stage_release(inputs, out)
            if arguments.phase == "stage":
                lock.abandon()
                lock = None
        if arguments.phase in ("seal", "all"):
            if lock is None:
                lock = WriterLock(out, create=False)
                lock.acquire()
            receipt = seal_candidate(out, arguments.receipt, arguments.git_sha)
            print(receipt.verification.candidate_id)
            lock.release()
            lock = None
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.abandon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
