"""Content-addressed replay cache for the dialogue analyzers (Todo-33 pattern).

Cache key = sha256(analyzer_version + wav sha + transcript content hash +
declared stream facts + frozen constants hash). An entry stores the exact
artifact bytes plus the binding record; a key occupied by an entry whose
binding differs (or whose payload is unreadable) is an explicit conflict —
overwrite is refused, never silently reused or replaced. Any change to a
frozen constant changes the key, so stale thresholds can never be served.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from services.analyze.analysis_models import AnalyzeRequestError, StreamFacts
from services.foundation_io import atomic_write, canonical_model_bytes

BINDING_FILE = "binding.json"
ARTIFACT_FILE = "analysis-artifact.json"


class AnalyzeCacheConflictError(AnalyzeRequestError):
    """A stale/foreign cache entry occupies the key; overwrite refused."""

    label = "analyze_cache_conflict"


class AnalyzerCacheBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    analyzer_version: str
    wav_sha256: str
    transcript_sha256: str
    declared: StreamFacts
    frozen_constants_hash: str
    wav_path: str


def cache_key(
    *,
    analyzer_version: str,
    wav_sha256: str,
    transcript_sha256: str,
    declared: StreamFacts,
    frozen_constants_hash: str,
) -> str:
    material = "\n".join(
        (
            analyzer_version,
            wav_sha256,
            transcript_sha256,
            str(declared.sample_rate),
            str(declared.channels),
            declared.codec,
            frozen_constants_hash,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


class AnalyzerCache:
    """Deterministic replay store; single-writer, no eviction, no overwrite."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def entry_dir(self, key: str) -> Path:
        return self.root / key

    def lookup(self, key: str, binding: AnalyzerCacheBinding) -> bytes | None:
        entry = self.entry_dir(key)
        if not entry.exists():
            return None
        binding_path = entry / BINDING_FILE
        if not binding_path.is_file():
            raise AnalyzeCacheConflictError(
                f"stale cache entry at {entry}: binding record missing; overwrite refused"
            )
        try:
            stored = AnalyzerCacheBinding.model_validate_json(binding_path.read_bytes())
        except (OSError, ValidationError) as error:
            raise AnalyzeCacheConflictError(
                f"stale cache entry at {entry}: binding record unreadable; overwrite refused"
            ) from error
        if stored != binding:
            raise AnalyzeCacheConflictError(
                f"foreign cache entry at {entry}: binding differs; overwrite refused"
            )
        try:
            return (entry / ARTIFACT_FILE).read_bytes()
        except OSError as error:
            raise AnalyzeCacheConflictError(
                f"stale cache entry at {entry}: payload unreadable; overwrite refused"
            ) from error

    def store(self, key: str, binding: AnalyzerCacheBinding, artifact_bytes: bytes) -> None:
        entry = self.entry_dir(key)
        existing = self.lookup(key, binding)
        if existing is not None:
            if existing != artifact_bytes:
                raise AnalyzeCacheConflictError(
                    f"cache entry at {entry} reproduced different bytes; overwrite refused"
                )
            return
        atomic_write(entry / ARTIFACT_FILE, artifact_bytes)
        # binding is the completeness marker: written last
        atomic_write(entry / BINDING_FILE, canonical_model_bytes(binding))
