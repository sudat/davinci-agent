"""YouTube reference ingestion — task 25."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from services.contracts.primitives import StrictModel
from services.reference_learning.ingest import ReferenceIngestError
from services.reference_learning.models import ReferenceLibraryV1, ReferenceSourceV1

_FALLBACK_MESSAGE = (
    "YouTube reference is unavailable via a compliant path. "
    "ローカルファイルを提供してください / please provide a local file instead."
)

_YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)",
    re.IGNORECASE,
)


class YoutubeReferenceUnavailable(ReferenceIngestError):  # noqa: N818
    """Typed error with local-file fallback request."""

    def __init__(self, url: str, message: str | None = None) -> None:
        detail = message or f"{_FALLBACK_MESSAGE} (url={url})"
        need_jp = "ローカルファイルを提供してください" not in detail
        need_en = "please provide a local file" not in detail
        if need_jp and need_en:
            detail = f"{detail} {_FALLBACK_MESSAGE}"
        super().__init__(detail)
        self.url = url


class YoutubeCompliantConfig(StrictModel):
    """Typed config for a compliant YouTube acquisition/analysis path."""

    tool_path: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_valid_youtube_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return bool(_YOUTUBE_URL_RE.match(url.strip()))


def _resolve_config(config: YoutubeCompliantConfig | None) -> YoutubeCompliantConfig:
    if config is not None:
        return config
    env_path = os.environ.get("YOUTUBE_COMPLIANT_TOOL_PATH") or os.environ.get(
        "REFERENCE_YOUTUBE_TOOL_PATH"
    )
    if env_path:
        return YoutubeCompliantConfig(tool_path=env_path)
    return YoutubeCompliantConfig(tool_path=None)


def _has_compliant_path(cfg: YoutubeCompliantConfig) -> bool:
    if cfg.tool_path is None or cfg.tool_path.strip() == "":
        return False
    p = Path(cfg.tool_path)
    return p.exists() and p.is_file()


class YoutubeIngestResult(NamedTuple):
    """A newly registered source and the library version that includes it."""

    source: ReferenceSourceV1
    library: ReferenceLibraryV1


def ingest_youtube_reference(
    url: str,
    *,
    library: ReferenceLibraryV1,
    config: YoutubeCompliantConfig | None = None,
    source_id: str | None = None,
    created_at: str | None = None,
) -> YoutubeIngestResult:
    """Ingest a YouTube URL only via a compliant path.

    Raises YoutubeReferenceUnavailable with a local-file request message when
    no compliant path is configured or the URL is malformed. Never scrapes.
    """
    if not _is_valid_youtube_url(url):
        raise YoutubeReferenceUnavailable(
            url, f"Invalid YouTube URL: {url}. {_FALLBACK_MESSAGE}"
        )

    cfg = _resolve_config(config)

    if not _has_compliant_path(cfg):
        raise YoutubeReferenceUnavailable(url)

    base_id = f"ref-yt-{abs(hash(url)) % 10_000_000:07d}"
    resolved_source_id = source_id if source_id is not None else base_id
    if not resolved_source_id or not re.match(
        r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", resolved_source_id
    ):
        resolved_source_id = base_id

    resolved_created_at = created_at if created_at is not None else _now_iso()

    source = ReferenceSourceV1(
        source_id=resolved_source_id,
        kind="youtube_url",
        location=url,
        created_at=resolved_created_at,
        provenance=library.provenance,
        sha256=None,
    )

    new_library = library.model_copy(
        update={
            "version": library.version + 1,
            "sources": (*library.sources, source),
        }
    )
    return YoutubeIngestResult(source=source, library=new_library)
