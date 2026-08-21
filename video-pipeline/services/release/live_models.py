"""Typed contracts for the live fault-injecting replay (Todo 67 / F3).

Every observation the live replay records is a strict, canonical-serializable
model: typed injection outcomes, the declared-vs-measured render policy,
anchor frame hashes, A/B profile-swap evidence, and the run summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from services.contracts.primitives import Sha256, StrictModel

AnchorPosition = Literal["frame-0", "frame-25pct", "frame-50pct", "frame-75pct", "frame-last"]
InjectionRoute = Literal[
    "partial-build",
    "resolve-restart",
    "stale-state",
    "false-success",
    "repeated-interruption",
]
INJECTION_ROUTES: tuple[InjectionRoute, ...] = (
    "partial-build",
    "resolve-restart",
    "stale-state",
    "false-success",
    "repeated-interruption",
)
ProfileId = Literal["a", "b"]
PROFILE_SNAPSHOT_IDS: dict[str, str] = {"a": "p3-brand-a", "b": "p3-brand-b"}


class AnchorFrame(StrictModel):
    """One anchor frame's deterministic image-file hash (bmp: pinned ffmpeg
    builds ship no png encoder, and bmp extraction is byte-stable)."""

    position: AnchorPosition
    frame_index: int
    image_format: Literal["bmp"] = "bmp"
    frame_sha256: Sha256


class RenderPolicy(StrictModel):
    """The declared render contract; measured facts must equal it exactly."""

    schema_version: Literal["render-policy-v1"] = "render-policy-v1"
    container: str
    video_codec: str
    audio_codec: str
    width: int
    height: int
    frame_rate: str
    frame_count: int
    duration_ms: int
    duration_tolerance_ms: int = 0
    audio_channels: int
    audio_sample_rate_hz: int
    audio_layout: str


class FinalRenderObservation(StrictModel):
    path: str
    sha256: Sha256
    source_profile: ProfileId
    anchors: tuple[AnchorFrame, ...]
    anchor_extraction_argv: tuple[str, ...]
    policy_declared: RenderPolicy
    policy_measured: RenderPolicy
    policy_verified: bool


class InjectionOutcome(StrictModel):
    route: InjectionRoute
    outcome: str
    passed: bool
    detail: str
    evidence_dir: str


class ProfileBuildEvidence(StrictModel):
    profile: ProfileId
    snapshot_id: str
    structure_sha256: Sha256
    structure_rows: int
    manifest_sha256: Sha256
    profile_snapshot_sha256: Sha256
    asset_sha256: dict[str, Sha256]
    derivatives: dict[str, Sha256]
    render_path: str
    render_sha256: Sha256


class ProfileSwapEvidence(StrictModel):
    profiles: tuple[ProfileBuildEvidence, ...]
    structural_equal: bool
    presentation_differences: tuple[str, ...]
    passed: bool


class LeaseEvidence(StrictModel):
    db_path: str
    resource: str
    holder: str
    ttl_seconds: int
    acquired: bool
    released: bool


class H1Evidence(StrictModel):
    binding_path: str
    binding_sha256: Sha256
    episode_id: str
    git_sha: str


class RestartObservation(StrictModel):
    quit_method: str
    down_probe_error: str
    relaunch_seconds: int
    binding_same: bool


@dataclass(frozen=True)
class ProfileBuildResult:
    """Runtime carrier from the profile-build seam (not serialized)."""

    render_path: Path
    derivatives: dict[str, str] = field(default_factory=dict)


class LiveReplaySummary(StrictModel):
    schema_version: Literal["live-replay-summary-v1"] = "live-replay-summary-v1"
    mode: Literal["live"] = "live"
    extract_path: str
    extract_git_sha: str
    candidate_id: Sha256
    h1: H1Evidence
    lease: LeaseEvidence
    injections: tuple[InjectionOutcome, ...]
    profile_swap: ProfileSwapEvidence
    final_render: FinalRenderObservation
    verdict: Literal["passed", "failed"]
    failure_codes: tuple[str, ...]


__all__ = [
    "INJECTION_ROUTES",
    "PROFILE_SNAPSHOT_IDS",
    "AnchorFrame",
    "AnchorPosition",
    "FinalRenderObservation",
    "H1Evidence",
    "InjectionOutcome",
    "InjectionRoute",
    "LeaseEvidence",
    "LiveReplaySummary",
    "ProfileBuildEvidence",
    "ProfileBuildResult",
    "ProfileId",
    "ProfileSwapEvidence",
    "RenderPolicy",
    "RestartObservation",
]
