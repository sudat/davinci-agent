"""Deterministic presentation-intent consumption (r9e gap).

Applied NL presentation commands (``subtitle_shorter`` etc.) journal an
intent via :mod:`services.episode_cockpit.review_chat`, but nothing consumed
it — the rebuild re-rendered the unchanged plan, so the operator's fix
published a content-identical re-encode. This module is the deterministic
(no-LLM) translation layer: presentation intents become render/compile
settings overrides, derived cumulatively in journal order and bounded to
the presentation domain. Every mapping is recorded with its command id
(intent → setting → render traceability); kinds with no review-plane knob
are honestly-unimplemented typed notes, never fake effects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.review_chat import (
    APPLIED_COMMANDS_NAME,
    AppliedCommand,
    ReviewChatError,
    ReviewCommandKind,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.models import (
    PresentationRenderSettings,
    TracePresentation,
    TracePresentationNote,
)

type PresentationSettingKind = Literal["subtitle_max_chars_per_line", "bgm_gain_mb"]

# The subtitle base width is the frozen Phase-1 QC policy
# (``services/cli/compile_ir.py:qc_policy`` ``max_chars_per_line=20``);
# each ``subtitle_shorter`` steps it down, floored so cues stay renderable.
SUBTITLE_BASE_CHARS_PER_LINE: int = 20
SUBTITLE_SHORTER_STEP_CHARS: int = 4
SUBTITLE_MIN_CHARS_PER_LINE: int = 8
# BGM gain steps are integer millibels on the music-anchor knob
# (``services/presentation/audio_models.py:AudioAnchor.gain_mb``);
# each ``lower_bgm`` deepens the cut, floored at -6 dB.
LOWER_BGM_STEP_MB: int = -600
LOWER_BGM_FLOOR_MB: int = -6000

PRESENTATION_KINDS: frozenset[ReviewCommandKind] = frozenset(
    {
        "subtitle_shorter",
        "remove_effect",
        "lower_bgm",
        "match_color",
        "channel_lower_third",
    }
)

_UNIMPLEMENTED_DETAILS: dict[ReviewCommandKind, str] = {
    "remove_effect": (
        "the review-plane preview applies no zoom/transform effect "
        "(transforms run at resolve_build via ZoomX/ZoomY, beyond the "
        "PREVIEW_READY stop), so there is no effect to remove here"
    ),
    "match_color": (
        "color preset selection is driven by declared camera/colorimetry "
        "metadata (services/presentation/color_models.py), which carries "
        "no per-command knob on the review-plane path"
    ),
    "channel_lower_third": (
        "channel-scope overlay titles need registry assets plus probe-verified "
        "placement (services/presentation/overlay_models.py), which the "
        "compile/preview re-entry cannot provision; recorded, not rendered"
    ),
}


class PresentationTranslationError(CockpitUnprocessableError):
    """Typed presentation-override derivation/journal failure."""


class PresentationSettingOverride(StrictModel):
    """One applied presentation command's effect on a named render setting."""

    command_id: Identifier
    command_kind: ReviewCommandKind
    setting: PresentationSettingKind
    value: int = Field(strict=True)


class UnimplementedPresentationNote(StrictModel):
    """An honestly-unimplemented presentation kind: recorded, never faked."""

    command_id: Identifier
    command_kind: ReviewCommandKind
    code: Literal["no-review-plane-knob"] = "no-review-plane-knob"
    detail: str = Field(min_length=1, strict=True)


class PresentationOverrideSet(StrictModel):
    """Cumulative presentation overrides in journal order (presentation only)."""

    schema_version: Literal["cockpit-presentation-overrides-v1"] = (
        "cockpit-presentation-overrides-v1"
    )
    overrides: tuple[PresentationSettingOverride, ...] = ()
    notes: tuple[UnimplementedPresentationNote, ...] = ()

    def is_empty(self) -> bool:
        return not self.overrides and not self.notes

    def applied_command_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *(entry.command_id for entry in self.overrides),
                    *(entry.command_id for entry in self.notes),
                )
            )
        )

    def effective_subtitle_max_chars(self) -> int | None:
        values = [
            entry.value
            for entry in self.overrides
            if entry.setting == "subtitle_max_chars_per_line"
        ]
        return values[-1] if values else None

    def effective_bgm_gain_mb(self) -> int | None:
        values = [
            entry.value for entry in self.overrides if entry.setting == "bgm_gain_mb"
        ]
        return values[-1] if values else None

    def to_render_settings(self) -> PresentationRenderSettings | None:
        subtitle = self.effective_subtitle_max_chars()
        bgm = self.effective_bgm_gain_mb()
        if subtitle is None and bgm is None:
            return None
        return PresentationRenderSettings(
            subtitle_max_chars_per_line=subtitle, bgm_gain_mb=bgm
        )

    def to_trace_presentation(self) -> TracePresentation | None:
        """Audit record for the preview trace (None for plain rebuilds)."""

        if self.is_empty():
            return None
        settings = self.to_render_settings()
        return TracePresentation(
            applied_command_ids=tuple(self.applied_command_ids()),
            subtitle_max_chars_per_line=(
                settings.subtitle_max_chars_per_line if settings is not None else None
            ),
            bgm_gain_mb=settings.bgm_gain_mb if settings is not None else None,
            notes=tuple(
                TracePresentationNote(
                    command_id=note.command_id,
                    command_kind=note.command_kind,
                    code=note.code,
                    detail=note.detail,
                )
                for note in self.notes
            ),
        )


class CompilePresentationManifest(StrictModel):
    """Compile-manifest record: which intents set which settings (audit)."""

    schema_version: Literal["cockpit-compile-presentation-v1"] = (
        "cockpit-compile-presentation-v1"
    )
    head_version: int = Field(ge=1, strict=True)
    plan_version: str = Field(min_length=1, strict=True)
    applied_command_ids: tuple[Identifier, ...] = ()
    overrides: PresentationOverrideSet
    recorded_at: str = Field(min_length=1, strict=True)


def derive_presentation_overrides(
    commands: tuple[AppliedCommand, ...],
) -> PresentationOverrideSet:
    """Translate applied commands to settings overrides (journal order).

    Cumulative and order-preserving: each ``subtitle_shorter`` steps the
    width down from the previous value; each ``lower_bgm`` deepens the
    gain cut down to the floor. Non-presentation domains (selection,
    edit_plan, scope) are ignored — never translated, never noted.
    """

    overrides: list[PresentationSettingOverride] = []
    notes: list[UnimplementedPresentationNote] = []
    subtitle_width = SUBTITLE_BASE_CHARS_PER_LINE
    bgm_gain = 0
    for command in commands:
        if command.command_kind not in PRESENTATION_KINDS:
            continue
        match command.command_kind:
            case "subtitle_shorter":
                subtitle_width = max(
                    subtitle_width - SUBTITLE_SHORTER_STEP_CHARS,
                    SUBTITLE_MIN_CHARS_PER_LINE,
                )
                overrides.append(
                    PresentationSettingOverride(
                        command_id=command.command_id,
                        command_kind=command.command_kind,
                        setting="subtitle_max_chars_per_line",
                        value=subtitle_width,
                    )
                )
            case "lower_bgm":
                bgm_gain = max(bgm_gain + LOWER_BGM_STEP_MB, LOWER_BGM_FLOOR_MB)
                overrides.append(
                    PresentationSettingOverride(
                        command_id=command.command_id,
                        command_kind=command.command_kind,
                        setting="bgm_gain_mb",
                        value=bgm_gain,
                    )
                )
            case _:
                notes.append(
                    UnimplementedPresentationNote(
                        command_id=command.command_id,
                        command_kind=command.command_kind,
                        detail=_UNIMPLEMENTED_DETAILS[command.command_kind],
                    )
                )
    return PresentationOverrideSet(overrides=tuple(overrides), notes=tuple(notes))


def load_journal_commands(episode_dir: Path) -> tuple[AppliedCommand, ...]:
    """Read the applied-command journal in append (journal) order."""

    log_path = episode_dir / APPLIED_COMMANDS_NAME
    try:
        lines = log_path.read_bytes().splitlines()
    except OSError:
        return ()
    commands: list[AppliedCommand] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            commands.append(AppliedCommand.model_validate_json(line))
        except ValueError as error:
            raise ReviewChatError(
                "applied-command-log-corrupt", f"unparsable line in {log_path}"
            ) from error
    return tuple(commands)


def presentation_manifest_name(head_version: int) -> str:
    return f"presentation-overrides-v{head_version}.json"


def write_presentation_manifest(
    plan_dir: Path,
    *,
    head_version: int,
    plan_version: str,
    override_set: PresentationOverrideSet,
) -> Path | None:
    """Record the override set beside plan/ir (audit); None when empty.

    Empty means no presentation intent exists, so nothing is written and
    plain rebuilds keep byte-identical compile inputs.
    """

    if override_set.is_empty():
        return None
    manifest = CompilePresentationManifest(
        head_version=head_version,
        plan_version=plan_version,
        applied_command_ids=tuple(override_set.applied_command_ids()),
        overrides=override_set,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    path = plan_dir / presentation_manifest_name(head_version)
    atomic_write(path, canonical_model_bytes(manifest))
    return path


def consume_presentation_intents(
    episode_dir: Path,
    plan_dir: Path,
    *,
    head_version: int,
    plan_version: str,
) -> PresentationOverrideSet:
    """Derive + manifest-record the journal's presentation intents.

    Idempotent re-derivation: the full journal is re-read every rebuild,
    so the result depends only on applied history, never on how many
    rebuilds already ran.
    """

    try:
        override_set = derive_presentation_overrides(load_journal_commands(episode_dir))
    except ReviewChatError as error:
        raise PresentationTranslationError(error.code, error.detail) from error
    write_presentation_manifest(
        plan_dir,
        head_version=head_version,
        plan_version=plan_version,
        override_set=override_set,
    )
    return override_set


__all__ = [
    "LOWER_BGM_FLOOR_MB",
    "LOWER_BGM_STEP_MB",
    "PRESENTATION_KINDS",
    "SUBTITLE_BASE_CHARS_PER_LINE",
    "SUBTITLE_MIN_CHARS_PER_LINE",
    "SUBTITLE_SHORTER_STEP_CHARS",
    "CompilePresentationManifest",
    "PresentationOverrideSet",
    "PresentationSettingKind",
    "PresentationSettingOverride",
    "PresentationTranslationError",
    "UnimplementedPresentationNote",
    "consume_presentation_intents",
    "derive_presentation_overrides",
    "load_journal_commands",
    "presentation_manifest_name",
    "write_presentation_manifest",
]
