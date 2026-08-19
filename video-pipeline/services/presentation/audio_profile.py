"""Compile the profile-driven audio section and choose the application ladder.

The compiler is deterministic and rights-gated: BGM/SE assets must be
registered, approved, unexpired, right-use, and byte-identical to the
registry hashes at Job time; anchors derive exactly from the profile's
tone/SE durations (a frame rate that cannot express them in whole frames
is a typed failure); the four logical roles own four distinct tracks; and
ducking is declared intent (dialogue-triggered, external mix only). The
ladder prefers the shared external mixed derivative, then a LIVE-VERIFIED
fixed preset (an unverified preset rung is a typed refusal, never a silent
downgrade), else the explicit manual fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.foundation_io import sha256_file
from services.presentation.asset_registry import (
    AssetRightsError,
    RegistrySnapshot,
    check_rights,
)
from services.presentation.audio_models import (
    LOGICAL_ROLES,
    AudioAnchor,
    AudioAssetRef,
    AudioLadderRung,
    AudioProfileSection,
    AudioTargets,
    DuckingDeclaration,
    LogicalAudioTrack,
    ProcessingAssignment,
)
from services.qc.models import AudioThresholds
from services.spike.gate_models import CapabilityMatrix

if TYPE_CHECKING:
    from services.presentation.models import ResolvedPresentationProfile
    from services.presentation.snapshot import JobPresentationSnapshot

DEFAULT_BGM_GAIN_MB: Final = -600
DEFAULT_SE_GAIN_MB: Final = -300
DEFAULT_FADE_FRAMES: Final = 15
DEFAULT_DIALOGUE_TRACK: Final = 1
DEFAULT_AMBIENT_TRACK: Final = 2
DEFAULT_MUSIC_TRACK: Final = 3
DEFAULT_SFX_TRACK: Final = 4
DEFAULT_LOUDNESS_TARGET_MLUFS: Final = -16000
DEFAULT_LOUDNESS_TOLERANCE_MLUFS: Final = 1000
DEFAULT_MAX_PEAK_MB: Final = -600
DEFAULT_CHANNELS: Final = 2
PRESET_FINDING: Final = "fairlight-track-preset-apply"
SETTING_FINDING: Final = "fairlight-audio-setting-roundtrip"


class AudioProfileError(ValueError):
    """Typed blocking audio-profile failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def _exact_frames(milliseconds: int, rate: RationalFrameRate, what: str) -> int:
    value = Fraction(milliseconds * rate.num, 1000 * rate.den)
    if value.denominator != 1:
        raise AudioProfileError(
            "anchor_frames_inexact",
            f"{what}: {milliseconds}ms is not a whole number of frames at "
            f"{rate.num}/{rate.den}",
        )
    return int(value)


def _asset_path(assets_root: Path, entry_path: str) -> Path:
    path = Path(entry_path)
    return path if path.is_absolute() else assets_root / path


def _approved_ref(
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    kind: Literal["tone", "se"],
    snapshot: JobPresentationSnapshot,
    assets_root: Path,
) -> AudioAssetRef:
    binding = next(
        (b for b in profile.asset_bindings if b.kind == kind), None
    )
    if binding is None:
        raise AudioProfileError(
            "rights_missing", f"the profile binds no {kind} asset"
        )
    try:
        entry = check_rights(
            registry,
            binding.asset_id,
            usage=kind,
            territory=snapshot.job_territory,
            at_job=snapshot.job_date,
        )
        observed = sha256_file(_asset_path(assets_root, entry.path))
        check_rights(
            registry,
            binding.asset_id,
            usage=kind,
            territory=snapshot.job_territory,
            at_job=snapshot.job_date,
            observed_sha256=observed,
        )
    except AssetRightsError as error:
        raise AudioProfileError(
            f"rights_{error.reason}", str(error)
        ) from error
    except OSError as error:
        raise AudioProfileError(
            "rights_missing", f"{kind} asset bytes are unreadable: {error}"
        ) from error
    return AudioAssetRef(
        kind=kind,
        asset_id=entry.asset_id,
        sha256=entry.sha256,
        path=_asset_path(assets_root, entry.path).as_posix(),
    )


def _assignments() -> tuple[ProcessingAssignment, ...]:
    return (
        ProcessingAssignment(kind="gain", role="music", on_track_role="music"),
        ProcessingAssignment(kind="fades", role="music", on_track_role="music"),
        ProcessingAssignment(kind="gain", role="sfx", on_track_role="sfx"),
        ProcessingAssignment(kind="ducking", role="music", on_track_role="music"),
        ProcessingAssignment(
            kind="loudness_normalization", role="dialogue", on_track_role="dialogue"
        ),
        ProcessingAssignment(
            kind="loudness_normalization", role="ambient", on_track_role="ambient"
        ),
    )


def compile_audio_profile(  # noqa: PLR0913 (compile contract fixed by the Todo-59 brief)
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    *,
    job_snapshot: JobPresentationSnapshot,
    assets_root: Path,
    total_frames: int,
    frame_rate: RationalFrameRate,
    dialogue_track: int = DEFAULT_DIALOGUE_TRACK,
    ambient_track: int = DEFAULT_AMBIENT_TRACK,
    music_track: int = DEFAULT_MUSIC_TRACK,
    sfx_track: int = DEFAULT_SFX_TRACK,
    targets: AudioTargets | None = None,
    bgm_gain_mb: int = DEFAULT_BGM_GAIN_MB,
    se_gain_mb: int = DEFAULT_SE_GAIN_MB,
    fade_frames: int = DEFAULT_FADE_FRAMES,
) -> AudioProfileSection:
    """Compile the hash-sealed audio section; every refusal is typed."""

    tone = _approved_ref(profile, registry, "tone", job_snapshot, assets_root)
    se = _approved_ref(profile, registry, "se", job_snapshot, assets_root)
    bgm_frames = _exact_frames(
        profile.audio.tone_duration_ms, frame_rate, "bgm anchor"
    )
    se_frames = _exact_frames(
        profile.audio.se_duration_ms, frame_rate, "se anchor"
    )
    if bgm_frames + se_frames > total_frames:
        raise AudioProfileError(
            "anchor_out_of_timeline",
            f"bgm({bgm_frames}) + se({se_frames}) frames exceed the "
            f"{total_frames}-frame timeline",
        )
    resolved_targets = targets or AudioTargets(
        loudness_target_mlufs=DEFAULT_LOUDNESS_TARGET_MLUFS,
        loudness_tolerance_mlufs=DEFAULT_LOUDNESS_TOLERANCE_MLUFS,
        max_peak_mb=DEFAULT_MAX_PEAK_MB,
        channels=DEFAULT_CHANNELS,
        sample_rate_hz=profile.audio.sample_rate_hz,
    )
    bgm_anchor = AudioAnchor(
        item_id="bgm.tone",
        role="music",
        asset_kind="tone",
        record_span=RecordFrameSpan(start_frame=0, end_frame=bgm_frames),
        offset_samples=0,
        gain_mb=bgm_gain_mb,
        fade_in_frames=fade_frames,
        fade_out_frames=fade_frames,
    )
    se_anchor = AudioAnchor(
        item_id="se.primary",
        role="sfx",
        asset_kind="se",
        record_span=RecordFrameSpan(
            start_frame=bgm_frames, end_frame=bgm_frames + se_frames
        ),
        offset_samples=0,
        gain_mb=se_gain_mb,
        fade_in_frames=0,
        fade_out_frames=0,
    )
    tracks = (
        LogicalAudioTrack(role="dialogue", resolve_track_index=dialogue_track),
        LogicalAudioTrack(role="ambient", resolve_track_index=ambient_track),
        LogicalAudioTrack(
            role="music", resolve_track_index=music_track, anchors=(bgm_anchor,)
        ),
        LogicalAudioTrack(
            role="sfx", resolve_track_index=sfx_track, anchors=(se_anchor,)
        ),
    )
    draft = AudioProfileSection(
        episode_id=profile.episode_id,
        profile_snapshot_sha256=profile.content_hash(),
        assets={"tone": tone, "se": se},
        logical_tracks=tracks,
        ducking=DuckingDeclaration(declared=True),
        assignments=_assignments(),
        targets=resolved_targets,
        total_frames=total_frames,
        frame_rate=frame_rate,
        section_sha256="0" * 64,
    )
    return draft.model_copy(update={"section_sha256": draft.content_hash()})


def verify_role_separation(section: AudioProfileSection) -> None:
    """Typed separation gate: dialogue processing never lands on ambient."""

    indices = [track.resolve_track_index for track in section.logical_tracks]
    roles = [track.role for track in section.logical_tracks]
    if len(set(indices)) != len(indices) or set(roles) != set(LOGICAL_ROLES):
        raise AudioProfileError(
            "role_conflation", "logical roles must own distinct tracks"
        )
    ducking = section.ducking
    trigger: object = (
        ducking.get("trigger_role") if isinstance(ducking, dict) else ducking.trigger_role
    )
    if trigger != "dialogue":
        raise AudioProfileError(
            "role_conflation",
            "ducking may be triggered by dialogue only, never ambient",
        )
    for assignment in section.assignments:
        if assignment.role != assignment.on_track_role:
            raise AudioProfileError(
                "role_conflation",
                f"{assignment.kind} processing for {assignment.role} was assigned "
                f"to the {assignment.on_track_role} track",
            )


@dataclass(frozen=True, slots=True)
class PresetSupport:
    """The fail-closed preset verdict derived from published probe findings."""

    verified: bool
    evidence_sha: str | None


def audio_preset_support_from_matrix(findings_path: Path) -> PresetSupport:
    if not findings_path.is_file():
        return PresetSupport(verified=False, evidence_sha=None)
    try:
        matrix = CapabilityMatrix.model_validate_json(findings_path.read_bytes())
    except OSError:
        return PresetSupport(verified=False, evidence_sha=None)
    rows = {finding.finding: finding for finding in matrix.findings}
    preset = rows.get(PRESET_FINDING)
    if preset is None or not preset.api_available or not preset.live_verified:
        return PresetSupport(verified=False, evidence_sha=None)
    evidence = preset.evidence_refs[0].sha256 if preset.evidence_refs else None
    return PresetSupport(verified=True, evidence_sha=evidence)


def choose_audio_rung(
    declared: AudioLadderRung | None,
    *,
    external_available: bool,
    preset: PresetSupport,
) -> AudioLadderRung:
    """Resolve the application ladder; unverifiable rungs are typed errors."""

    if declared == "verified_preset":
        if not preset.verified:
            raise AudioProfileError(
                "unverified_preset",
                "the preset rung requires a live-verified apply+readback probe; "
                "no Fairlight preset automation may run unverified",
            )
        return "verified_preset"
    if declared in (None, "external_mix_derivative"):
        if not external_available:
            raise AudioProfileError(
                "plugin_unavailable",
                "the external mix derivative requires the pinned ffmpeg mix "
                "filters (amix/sidechaincompress family) and they are unavailable",
            )
        return "external_mix_derivative"
    return "manual_fallback"


def audio_thresholds_from_targets(targets: AudioTargets) -> AudioThresholds:
    """Map the section targets onto the Todo-52 audio QC thresholds."""

    return AudioThresholds(
        max_peak_mb=targets.max_peak_mb,
        loudness_min_mlufs=targets.loudness_target_mlufs
        - targets.loudness_tolerance_mlufs,
        loudness_max_mlufs=targets.loudness_target_mlufs
        + targets.loudness_tolerance_mlufs,
        max_silence_ms=120000,
        expected_channels=targets.channels,
    )


__all__ = [
    "DEFAULT_AMBIENT_TRACK",
    "DEFAULT_BGM_GAIN_MB",
    "DEFAULT_DIALOGUE_TRACK",
    "DEFAULT_FADE_FRAMES",
    "DEFAULT_MUSIC_TRACK",
    "DEFAULT_SE_GAIN_MB",
    "DEFAULT_SFX_TRACK",
    "PRESET_FINDING",
    "SETTING_FINDING",
    "AudioProfileError",
    "PresetSupport",
    "audio_preset_support_from_matrix",
    "audio_thresholds_from_targets",
    "choose_audio_rung",
    "compile_audio_profile",
    "verify_role_separation",
]
