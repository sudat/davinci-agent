"""Content-addressed replay cache for the pinned ASR adapter.

Cache key = sha256(model_sha256 + cli_sha256 + wav_sha256 + frozen whisper
flag set). Each entry stores the exact artifact bytes, the raw CLI evidence
bytes, and the full request binding. A key occupied by an entry whose binding
differs from the current request (or whose payload is unreadable) is an
explicit error — overwrite is refused, never silently reused or replaced.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from services.analyze.asr_models import AsrCacheConflictError, Sha256
from services.foundation_io import atomic_write, canonical_model_bytes

BINDING_FILE = "binding.json"
ARTIFACT_FILE = "transcript-artifact.json"
RAW_FILE = "whisper-cli-output.json"


class CacheBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_sha256: Sha256
    cli_sha256: Sha256
    wav_sha256: Sha256
    whisper_argv_template: tuple[str, ...]
    media_sha256: Sha256
    media_path: str


class CachedTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_bytes: bytes
    raw_evidence_bytes: bytes


def cache_key(
    model_sha256: str,
    cli_sha256: str,
    wav_sha256: str,
    whisper_argv_template: tuple[str, ...],
) -> str:
    material = "\n".join((model_sha256, cli_sha256, wav_sha256, *whisper_argv_template))
    return hashlib.sha256(material.encode()).hexdigest()


class AsrCache:
    """Deterministic replay store; single-writer, no eviction, no overwrite."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def entry_dir(self, key: str) -> Path:
        return self.root / key

    def lookup(self, key: str, binding: CacheBinding) -> CachedTranscript | None:
        entry = self.entry_dir(key)
        if not entry.exists():
            return None
        binding_path = entry / BINDING_FILE
        if not binding_path.is_file():
            raise AsrCacheConflictError(
                f"stale cache entry at {entry}: binding record missing; overwrite refused"
            )
        try:
            stored = CacheBinding.model_validate_json(binding_path.read_bytes())
        except (OSError, ValidationError) as error:
            raise AsrCacheConflictError(
                f"stale cache entry at {entry}: binding record unreadable; overwrite refused"
            ) from error
        if stored != binding:
            raise AsrCacheConflictError(
                f"foreign cache entry at {entry}: binding differs; overwrite refused"
            )
        try:
            artifact_bytes = (entry / ARTIFACT_FILE).read_bytes()
            raw_evidence_bytes = (entry / RAW_FILE).read_bytes()
        except OSError as error:
            raise AsrCacheConflictError(
                f"stale cache entry at {entry}: payload unreadable; overwrite refused"
            ) from error
        return CachedTranscript(
            artifact_bytes=artifact_bytes, raw_evidence_bytes=raw_evidence_bytes
        )

    def store(
        self,
        key: str,
        binding: CacheBinding,
        artifact_bytes: bytes,
        raw_evidence_bytes: bytes,
    ) -> None:
        entry = self.entry_dir(key)
        existing = self.lookup(key, binding)
        if existing is not None:
            if (
                existing.artifact_bytes != artifact_bytes
                or existing.raw_evidence_bytes != raw_evidence_bytes
            ):
                raise AsrCacheConflictError(
                    f"cache entry at {entry} reproduced different bytes; overwrite refused"
                )
            return
        atomic_write(entry / ARTIFACT_FILE, artifact_bytes)
        atomic_write(entry / RAW_FILE, raw_evidence_bytes)
        # binding is the completeness marker: written last
        atomic_write(entry / BINDING_FILE, canonical_model_bytes(binding))
