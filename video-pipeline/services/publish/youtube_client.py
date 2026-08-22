"""OAuth-backed YouTube uploader seam — runs outside model context (task 54).

The uploader enforces the publish safety contract before any network
activity: public visibility requires a publication approval that matches
the package's ``publication_approval_ref``, the credential alias must
resolve to a credential file (path only — token material is never read,
stored, or logged here), and every upload is bound to the package's
idempotency key via the append-only ``UploadLedger``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol

from pydantic import Field

from services.contracts.primitives import StrictModel

if TYPE_CHECKING:
    from services.publish.idempotency import UploadLedger
    from services.publish.models import PublishPackageV1

Visibility = Literal["private", "unlisted", "public"]
CredentialLookup = Callable[[str], Path]

_TOKEN_MARKERS: Final = ("ya29.", "Bearer", "AIza", "eyJ")


class YoutubeClientError(RuntimeError):
    """Typed base for uploader failures."""


class PublicationApprovalRequiredError(YoutubeClientError):
    """Public visibility was requested without a matching publication approval."""


class CredentialResolutionError(YoutubeClientError):
    """A credential alias did not resolve to an existing credential file."""


class LiveUploadNotImplementedError(YoutubeClientError):
    """The real OAuth HTTP transport is not implemented — Operator runs it."""


class UploadSessionResult(StrictModel):
    """Transport-level result of one resumable upload session."""

    video_id: Annotated[str, Field(min_length=1, strict=True)]


class UploadResult(StrictModel):
    """Uploader-level outcome — ``reused`` marks an already-existing video."""

    video_id: str
    idempotency_key: str
    reused: bool


class YoutubeTransport(Protocol):
    """Seam to the YouTube API — mocked in tests, HttpTransport in production."""

    def upload_session(
        self,
        media_path: Path,
        metadata: Mapping[str, object],
        visibility: Visibility,
    ) -> UploadSessionResult: ...

    def already_uploaded(self, idempotency_key: str) -> str | None: ...


def default_credential_lookup(alias: str) -> Path:
    """Map a credential alias to a path under the user config directory.

    Path only: this function never reads, returns, or embeds token material.
    """

    return Path.home() / ".config" / "davinci-agent" / "credentials" / f"{alias}.json"


class HttpTransport:
    """Placeholder for the real OAuth HTTP transport (task 54 scope: no API calls).

    Holds only the injectable alias→path lookup a real implementation would
    use to load credentials at call time; no credential material in code.
    """

    def __init__(
        self,
        *,
        credential_lookup: CredentialLookup = default_credential_lookup,
    ) -> None:
        self._credential_lookup = credential_lookup

    def credential_path_for(self, alias: str) -> Path:
        return self._credential_lookup(alias)

    def upload_session(
        self,
        _media_path: Path,
        _metadata: Mapping[str, object],
        _visibility: Visibility,
    ) -> UploadSessionResult:
        raise LiveUploadNotImplementedError(
            "live YouTube upload is executed by the Operator, not by the pipeline",
        )

    def already_uploaded(self, _idempotency_key: str) -> str | None:
        raise LiveUploadNotImplementedError(
            "live YouTube lookup is executed by the Operator, not by the pipeline",
        )


def _metadata_for(package: PublishPackageV1) -> dict[str, object]:
    return {
        "episode_id": package.episode_id,
        "idempotency_key": package.idempotency_key,
        "title": package.selected_title,
        "description": package.description,
        "tags": list(package.tags),
        "chapters": [chapter.model_dump() for chapter in package.chapters],
        "playlist": package.playlist_target,
        "schedule_time": package.schedule_time,
    }


class YoutubeUploader:
    """Approval-bound, idempotent uploader over an injectable transport."""

    def __init__(
        self,
        transport: YoutubeTransport,
        ledger: UploadLedger,
        *,
        credential_lookup: CredentialLookup = default_credential_lookup,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self._credential_lookup = credential_lookup

    def upload(
        self,
        package: PublishPackageV1,
        media_path: Path,
        *,
        publication_approval: str | None = None,
    ) -> UploadResult:
        self._require_publication_approval(package, publication_approval)
        self._resolve_credential(package.channel_target)

        existing = self._ledger.completed_video_id(package.idempotency_key)
        if existing is not None:
            return UploadResult(
                video_id=existing,
                idempotency_key=package.idempotency_key,
                reused=True,
            )

        remote = self._transport.already_uploaded(package.idempotency_key)
        if remote is not None:
            self._ledger.complete(package.idempotency_key, remote)
            return UploadResult(
                video_id=remote,
                idempotency_key=package.idempotency_key,
                reused=True,
            )

        self._ledger.start(package.idempotency_key)
        try:
            session = self._transport.upload_session(
                media_path,
                _metadata_for(package),
                package.visibility,
            )
        except Exception as error:
            # Exception class name only: messages could echo request material.
            self._ledger.fail(package.idempotency_key, reason=type(error).__name__)
            raise
        self._ledger.complete(package.idempotency_key, session.video_id)
        return UploadResult(
            video_id=session.video_id,
            idempotency_key=package.idempotency_key,
            reused=False,
        )

    def _require_publication_approval(
        self,
        package: PublishPackageV1,
        publication_approval: str | None,
    ) -> None:
        if package.visibility != "public":
            return
        required_ref = package.publication_approval_ref
        if (
            publication_approval is None
            or required_ref is None
            or publication_approval != required_ref
        ):
            raise PublicationApprovalRequiredError(
                "public visibility requires a publication approval matching "
                "the package publication_approval_ref",
            )

    def _resolve_credential(self, channel_alias: str) -> None:
        credential_path = self._credential_lookup(channel_alias)
        for marker in _TOKEN_MARKERS:
            if marker in str(credential_path):
                raise CredentialResolutionError(
                    "credential lookup returned a token-like path",
                )
        if not credential_path.is_file():
            raise CredentialResolutionError(
                f"credential alias {channel_alias!r} did not resolve to a file: {credential_path}",
            )


__all__ = [
    "CredentialLookup",
    "CredentialResolutionError",
    "HttpTransport",
    "LiveUploadNotImplementedError",
    "PublicationApprovalRequiredError",
    "UploadResult",
    "UploadSessionResult",
    "Visibility",
    "YoutubeClientError",
    "YoutubeTransport",
    "YoutubeUploader",
    "default_credential_lookup",
]
