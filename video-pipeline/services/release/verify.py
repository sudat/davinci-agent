"""Candidate verification core shared by build/verify/extract/finalize.

``verify_candidate`` recomputes the manifest, identity, source tree, staged
input gates, and (when required) read-only mode bits. Every failure raises a
typed :class:`ReleaseGateError`; nothing mutates the candidate.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.release.errors import ReleaseGateError
from services.release.input_gates import (
    check_gate_evidence,
    inputs_dir_of,
    load_h1_binding,
    scan_inputs,
)
from services.release.manifest import (
    MANIFEST_NAME,
    Manifest,
    build_manifest,
    candidate_id,
    manifest_bytes,
    tree_hash,
)
from services.release.models import (
    IDENTITY_NAME,
    SOURCE_DIR,
    CandidateVerification,
    ModeBits,
    ReleaseIdentity,
)


def _octal(modes: set[int]) -> str:
    return ",".join(f"{mode:04o}" for mode in sorted(modes)) if modes else "none"


def inspect_mode_bits(root: Path) -> ModeBits:
    """Walk ``root`` no-follow and report write bits on files and directories."""

    writable: list[str] = []
    file_modes: set[int] = set()
    dir_modes: set[int] = set()
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ReleaseGateError("writable_candidate", f"candidate root is not a directory: {root}")
    dir_modes.add(stat.S_IMODE(root_stat.st_mode))
    if root_stat.st_mode & 0o222:
        writable.append(".")
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as iterator:
            for entry in sorted(iterator, key=lambda item: item.name):
                if entry.is_symlink():
                    raise ReleaseGateError(
                        "writable_candidate", f"symlink inside candidate: {entry.path}"
                    )
                child_stat = entry.stat(follow_symlinks=False)
                relative = Path(entry.path).relative_to(root).as_posix()
                if stat.S_ISDIR(child_stat.st_mode):
                    dir_modes.add(stat.S_IMODE(child_stat.st_mode))
                    if child_stat.st_mode & 0o222:
                        writable.append(relative)
                    stack.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    raise ReleaseGateError(
                        "writable_candidate", f"non-regular file inside candidate: {entry.path}"
                    )
                file_modes.add(stat.S_IMODE(child_stat.st_mode))
                if child_stat.st_mode & 0o222:
                    writable.append(relative)
    return ModeBits(
        files_octal=_octal(file_modes),
        directories_octal=_octal(dir_modes),
        writable_paths=tuple(sorted(set(writable))),
        all_readonly=not writable,
    )


def read_manifest(root: Path) -> tuple[Manifest, bytes]:
    """Parse ``manifest.json`` strictly; malformed manifests are stale hashes."""

    path = root / MANIFEST_NAME
    if not path.is_file():
        raise ReleaseGateError("stale_hash", "manifest.json is missing")
    payload = path.read_bytes()
    try:
        manifest = Manifest.model_validate_json(payload)
    except ValidationError as error:
        raise ReleaseGateError(
            "stale_hash", f"manifest fails manifest-v1 validation: {error}"
        ) from error
    return manifest, payload


def _load_identity(root: Path) -> ReleaseIdentity:
    path = root / IDENTITY_NAME
    if not path.is_file():
        raise ReleaseGateError("stale_hash", "identity.json is missing")
    try:
        return ReleaseIdentity.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise ReleaseGateError("stale_hash", f"identity.json unparseable: {error}") from error


def verify_candidate(
    root: Path,
    *,
    expected_git_sha: str | None = None,
    require_h1_binding: bool = False,
    recompute: bool = False,
    require_readonly: bool = False,
) -> CandidateVerification:
    """Verify one candidate tree; returns a canonical verification record."""

    manifest, payload = read_manifest(root)
    if recompute and manifest_bytes(build_manifest(root)) != payload:
        raise ReleaseGateError(
            "stale_hash", "recomputed manifest differs from staged manifest bytes"
        )
    identity = _load_identity(root)
    if expected_git_sha is not None and identity.git_sha != expected_git_sha:
        raise ReleaseGateError(
            "stale_hash",
            f"candidate git sha {identity.git_sha} != expected {expected_git_sha}",
        )
    source_root = root / SOURCE_DIR
    if not source_root.is_dir():
        raise ReleaseGateError("missing_release_input", "candidate lacks source/ snapshot")
    observed_tree = tree_hash(source_root)
    if observed_tree != identity.source_tree_sha256:
        raise ReleaseGateError("stale_hash", "source tree drifts from identity")
    inputs_dir = inputs_dir_of(root)
    check_gate_evidence(inputs_dir)
    scan_inputs(inputs_dir)
    if require_h1_binding:
        binding = load_h1_binding(inputs_dir)
        if binding.git_sha != identity.git_sha:
            raise ReleaseGateError("stale_hash", "H1 binding git sha drifts from identity")
    modes = inspect_mode_bits(root)
    if require_readonly and not modes.all_readonly:
        raise ReleaseGateError(
            "writable_candidate",
            f"writable entries remain: {', '.join(modes.writable_paths[:5])}",
        )
    return CandidateVerification(
        candidate_path=str(root),
        candidate_id=candidate_id(manifest),
        manifest_sha256=sha256_file(root / MANIFEST_NAME),
        git_sha=identity.git_sha,
        source_tree_sha256=observed_tree,
        entry_count=len(manifest.entries),
        total_bytes=sum(entry.size for entry in manifest.entries),
        modes=modes,
    )


__all__ = [
    "inspect_mode_bits",
    "read_manifest",
    "verify_candidate",
]
