"""Synthetic job-tree builders for retention/GC tests.

Every tree lives under pytest ``tmp_path`` — the GC is never pointed at
real workspace directories (standing constraint for Todo 63).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from services.retention.gc import AUDIT_FILE_NAME
from services.retention.models import (
    JobRetentionState,
    RegisteredPath,
    RetentionRegistry,
)

NOW_EPOCH = 2_000_000_000
DAY_SECONDS = 86_400


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def policy_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "retention-policy-v1",
        "rebuildable_retention_days": 30,
        "authoritative_names": ("sources", "artifacts", "renders"),
        "manual_finalization_names": ("manual-finalization",),
        "rebuildable_names": ("proxies", "previews", "analysis", "contacts"),
        "runtime_cache_names": ("cache",),
    }
    payload.update(overrides)
    return payload


def frozen_job_files(job_id: str) -> dict[str, bytes]:
    """A representative tree: authoritative, manual-finalization, rebuildable, cache."""

    return {
        "sources/cam-a.mov": f"{job_id}-camera-original".encode(),
        "artifacts/selection-plan.json": f"{job_id}-selection-plan".encode(),
        "artifacts/edit-plan.json": f"{job_id}-edit-plan".encode(),
        "renders/final.mp4": f"{job_id}-final-render".encode(),
        "manual-finalization/frozen.json": f"{job_id}-freeze-state".encode(),
        "proxies/p1.mp4": f"{job_id}-proxy-1".encode(),
        "proxies/nested/p2.mp4": f"{job_id}-proxy-2".encode(),
        "previews/editorial.mp4": f"{job_id}-preview".encode(),
        "analysis/frames/f001.jpg": f"{job_id}-frame".encode(),
        "cache/asr/k1/transcript.json": f"{job_id}-asr".encode(),
    }


def write_job(  # noqa: PLR0913 (test fixture builder: one kwarg per tree option)
    root: Path,
    job_id: str,
    *,
    status: str = "FROZEN",
    frozen_at_epoch_s: int | None = NOW_EPOCH - 100 * DAY_SECONDS,
    files: dict[str, bytes] | None = None,
    with_state: bool = True,
    with_registry: bool = True,
    registry_entries: dict[str, str] | None = None,
) -> Path:
    """Materialize ``root/<job_id>`` with state file, registry, and payload files."""

    job_dir = root / job_id
    payload = frozen_job_files(job_id) if files is None else dict(files)
    for relative, content in payload.items():
        target = job_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if with_state:
        write_state(
            job_dir,
            job_id,
            status=status,
            frozen_at_epoch_s=frozen_at_epoch_s if status == "FROZEN" else None,
        )
    if with_registry:
        entries = registry_entries
        if entries is None:
            entries = {
                relative: hashlib.sha256(content).hexdigest()
                for relative, content in payload.items()
            }
        write_registry(job_dir, entries)
    return job_dir


def write_state(
    job_dir: Path,
    job_id: str,
    *,
    status: str,
    frozen_at_epoch_s: int | None = None,
) -> None:
    state = JobRetentionState.model_validate(
        {
            "schema_version": "retention-job-state-v1",
            "job_id": job_id,
            "status": status,
            "frozen_at_epoch_s": frozen_at_epoch_s,
        }
    )
    (job_dir / "job-state.json").write_bytes(state.model_dump_json().encode())


def write_registry(job_dir: Path, entries: dict[str, str]) -> None:
    registry = RetentionRegistry.mint(
        {path: RegisteredPath(sha256=digest) for path, digest in entries.items()}
    )
    (job_dir / "retention-registry.json").write_bytes(registry.model_dump_json().encode())


def read_registry(job_dir: Path) -> RetentionRegistry:
    return RetentionRegistry.model_validate_json(
        (job_dir / "retention-registry.json").read_bytes()
    )


def audit_records(root: Path) -> list[dict[str, object]]:
    audit = root / AUDIT_FILE_NAME
    if not audit.is_file():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines() if line]


def snapshot_tree(root: Path) -> dict[str, str]:
    """Kind+content digest of every entry under root (symlinks never followed)."""

    view: dict[str, str] = {}
    real_root = os.path.realpath(root)
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as scan:
            for entry in scan:
                relative = os.path.relpath(os.path.realpath(entry.path), real_root)
                if entry.is_symlink():
                    target = Path(entry.path).readlink()
                    view[relative] = f"symlink:{target}"
                elif entry.is_dir(follow_symlinks=False):
                    view[relative] = "dir"
                    stack.append(Path(entry.path))
                else:
                    digest = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
                    view[relative] = f"file:{digest}"
    return view
