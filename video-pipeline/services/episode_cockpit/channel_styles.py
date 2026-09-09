"""Channel style explicit save/restore — runtime channel state (工程3).

One ``channel-styles.json`` file at the episodes root holds every
channel's versioned style (multi-channel by key, never read/written
across channels), following the reference-library precedent
(``side_desks.py``: atomic_write + canonical_model_bytes + strict
models). This is runtime channel state, NEVER a new authoritative
artifact family (the consultation_store.py discipline): no
director/plan consumption reads it this wave, and the ONLY writers are
the explicit save/restore cockpit endpoints — adoption, rebuild, and
consultation flows never touch this file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, ValidationError

from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.errors import CockpitConflictError, CockpitUnprocessableError
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.foundation_io import atomic_write, canonical_model_bytes

CHANNEL_STYLES_NAME = "channel-styles.json"

_CHANNEL_ID: TypeAdapter[Identifier] = TypeAdapter(Identifier)

# Strict models never coerce list→tuple; pin the consultation_store convention.
type StringSequence = Annotated[tuple[str, ...], BeforeValidator(tuple)]


def now_stamp() -> str:
    return datetime.now(UTC).isoformat()


def validated_channel_id(channel_id: str) -> str:
    """Narrow a channel key to the Identifier contract or typed 422."""

    try:
        return _CHANNEL_ID.validate_python(channel_id)
    except ValidationError as error:
        raise CockpitUnprocessableError(
            "channel-unknown", f"channel id {channel_id!r} is not a valid identifier"
        ) from error


class ChannelStyleSourceV1(StrictModel):
    """Explicit provenance: which episode judgment the operator saved.

    Passed through exactly as the caller names it (explicit-source
    semantics — nothing is inferred); null means the operator composed
    the style by hand rather than from a judgment.
    """

    episode_id: Identifier
    judgment_id: str
    proposal_id: str | None = None


class ChannelStyleSaveRequest(StrictModel):
    """POST save body: every entry field except saved_at (server-stamped)."""

    name: str = Field(min_length=1)
    audience_message: str
    structure: str
    duration_estimate: str
    candidate_scenes: StringSequence = ()
    subtitle_policy: str
    audio_policy: str
    tempo_policy: str
    reference_mapping: str
    unused_reasons: str
    unconfirmed: StringSequence = ()
    note: str | None = None
    source: ChannelStyleSourceV1 | None = None


class ChannelStyleEntryV1(ChannelStyleSaveRequest):
    """Stored entry: request fields plus the server-stamped saved_at."""

    saved_at: str


class ChannelStyleVersionV1(StrictModel):
    """One version: monotonically increasing int plus its entry."""

    schema_version: Literal["cockpit-channel-style-version-v1"] = (
        "cockpit-channel-style-version-v1"
    )
    version: int = Field(ge=1)
    entry: ChannelStyleEntryV1
    saved_at: str


class ChannelStyleRecordV1(StrictModel):
    """One channel's version chain; current names the live version."""

    schema_version: Literal["cockpit-channel-style-record-v1"] = (
        "cockpit-channel-style-record-v1"
    )
    channel_id: Identifier
    versions: tuple[ChannelStyleVersionV1, ...] = ()
    current: int | None = None


class ChannelStyleLibraryV1(StrictModel):
    """The whole file: every channel's record keyed by channel id."""

    schema_version: Literal["cockpit-channel-styles-v1"] = "cockpit-channel-styles-v1"
    channels: dict[str, ChannelStyleRecordV1] = Field(default_factory=dict)


def _load_library_at(episodes_root: Path) -> ChannelStyleLibraryV1:
    path = episodes_root / CHANNEL_STYLES_NAME
    if not path.is_file():
        return ChannelStyleLibraryV1()
    try:
        return ChannelStyleLibraryV1.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise CockpitUnprocessableError(
            "channel-styles-corrupt", f"unparsable channel styles file {path}"
        ) from error


def _version_by_number(
    record: ChannelStyleRecordV1, number: int
) -> ChannelStyleVersionV1 | None:
    return next((item for item in record.versions if item.version == number), None)


def validate_style_pin(
    episodes_root: Path, *, channel: str, style_version: int | None
) -> ChannelStyleRecordV1:
    """Validate an intake pin: channel exists; the version exists for it."""

    key = validated_channel_id(channel)
    record = _load_library_at(episodes_root).channels.get(key)
    if record is None or not record.versions:
        raise CockpitUnprocessableError(
            "channel-unknown", f"channel {key} has no saved style versions"
        )
    if style_version is not None and _version_by_number(record, style_version) is None:
        raise CockpitUnprocessableError(
            "style-version-unknown",
            f"channel {key} has no style version {style_version}",
        )
    return record


class ChannelStyleOps(WorkspaceContext):
    """Explicit operator-saved channel styles (channel-file state only)."""

    def list_channels(self) -> dict[str, object]:
        library = _load_library_at(self._episodes_root)
        return {"channels": sorted(library.channels), "available": bool(library.channels)}

    def get_channel_style(self, channel_id: str) -> ChannelStyleRecordV1:
        key = validated_channel_id(channel_id)
        record = _load_library_at(self._episodes_root).channels.get(key)
        if record is None:
            return ChannelStyleRecordV1(channel_id=key)
        return record

    def save_channel_style(
        self, channel_id: str, body: ChannelStyleSaveRequest
    ) -> tuple[int, bool]:
        key = validated_channel_id(channel_id)
        stamp = now_stamp()
        entry = ChannelStyleEntryV1.model_validate({**body.model_dump(), "saved_at": stamp})
        library = _load_library_at(self._episodes_root)
        record = library.channels.get(key)
        if record is not None and record.current is not None:
            current = _version_by_number(record, record.current)
            if current is None:
                raise CockpitUnprocessableError(
                    "channel-styles-corrupt",
                    f"channel {key} points at missing version {record.current}",
                )
            if entry.model_copy(update={"saved_at": current.entry.saved_at}) == current.entry:
                return current.version, True
            next_number = max(item.version for item in record.versions) + 1
            versions = (
                *record.versions,
                ChannelStyleVersionV1(version=next_number, entry=entry, saved_at=stamp),
            )
        else:
            next_number = 1
            versions = (
                ChannelStyleVersionV1(version=next_number, entry=entry, saved_at=stamp),
            )
        channels = dict(library.channels)
        channels[key] = ChannelStyleRecordV1(
            channel_id=key, versions=versions, current=next_number
        )
        atomic_write(
            self._episodes_root / CHANNEL_STYLES_NAME,
            canonical_model_bytes(ChannelStyleLibraryV1(channels=channels)),
        )
        return next_number, False

    def restore_channel_style(self, channel_id: str, *, target_version: int) -> int:
        key = validated_channel_id(channel_id)
        library = _load_library_at(self._episodes_root)
        record = library.channels.get(key)
        if record is None or not record.versions:
            raise CockpitConflictError(
                "nothing-to-restore", f"channel {key} has no saved style versions"
            )
        target = _version_by_number(record, target_version)
        if target is None:
            raise CockpitUnprocessableError(
                "style-version-unknown",
                f"channel {key} has no style version {target_version}",
            )
        if record.current == target_version:
            return target_version
        stamp = now_stamp()
        next_number = max(item.version for item in record.versions) + 1
        versions = (
            *record.versions,
            ChannelStyleVersionV1(version=next_number, entry=target.entry, saved_at=stamp),
        )
        channels = dict(library.channels)
        channels[key] = ChannelStyleRecordV1(
            channel_id=key, versions=versions, current=next_number
        )
        atomic_write(
            self._episodes_root / CHANNEL_STYLES_NAME,
            canonical_model_bytes(ChannelStyleLibraryV1(channels=channels)),
        )
        return next_number


__all__ = [
    "CHANNEL_STYLES_NAME",
    "ChannelStyleEntryV1",
    "ChannelStyleLibraryV1",
    "ChannelStyleOps",
    "ChannelStyleRecordV1",
    "ChannelStyleSaveRequest",
    "ChannelStyleSourceV1",
    "ChannelStyleVersionV1",
    "now_stamp",
    "validate_style_pin",
    "validated_channel_id",
]
