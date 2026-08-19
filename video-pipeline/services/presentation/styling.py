"""Apply the resolved presentation profile to compiled cues (Todo 57).

Styling is appearance-only: cue text, wrapped lines, and record spans are
copied verbatim from the compiled production IR, and every titled item
passes the fact gate in :mod:`services.presentation.styling_gate`. The
result is sealed over canonical bytes and verified against the source IR
before it leaves; text/timing/style drift is a typed profile-scope failure,
never a silent restyle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from services.contracts.primitives import Producer
from services.contracts.serialization import GENESIS_SHA256, artifact_content_hash
from services.contracts.styled_presentation import (
    StyledCue,
    StyledPresentation,
    SubtitleStyleParams,
)
from services.contracts.timeline_ir import SubtitleCueItem
from services.presentation.styling_gate import build_titled_item
from services.presentation.styling_qc import require_styled_qc

if TYPE_CHECKING:
    from services.compile.subtitle_policy import SubtitleQcPolicy
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.presentation.asset_registry import (
        IsoDate,
        RegistrySnapshot,
        TerritoryCode,
    )
    from services.presentation.models import ResolvedPresentationProfile
    from services.presentation.styling_models import (
        FactEvidenceContext,
        TitledItemRequest,
    )

STYLE_PRODUCER: Final = Producer(name="presentation-stylist", version="todo57-v1")

type ProfileScopeReason = Literal[
    "text_changed",
    "timing_changed",
    "style_id_changed",
    "ir_binding_drift",
    "profile_binding_drift",
    "registry_binding_drift",
]


class ProfileScopeError(ValueError):
    """The styled output no longer matches its IR/profile/registry bindings."""

    def __init__(self, reason: ProfileScopeReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _ir_cues(ir: TimelineIrProduction) -> tuple[SubtitleCueItem, ...]:
    return tuple(
        sorted(
            (
                item
                for track in ir.tracks
                if track.track.kind == "subtitle"
                for item in track.items
                if isinstance(item, SubtitleCueItem)
            ),
            key=lambda cue: (cue.record_span.start_frame, cue.item_id),
        )
    )


def verify_styled_ir_binding(
    styled: StyledPresentation, ir: TimelineIrProduction
) -> None:
    """Fail typed unless the styled table binds this IR and copies text/timing."""

    if (
        styled.ir_artifact_id != ir.artifact_id
        or styled.ir_content_sha256 != ir.content_hash
    ):
        raise ProfileScopeError(
            "ir_binding_drift", "styled output is not bound to this timeline IR"
        )
    source_cues = {cue.item_id: cue for cue in _ir_cues(ir)}
    if set(source_cues) != {cue.item_id for cue in styled.cues}:
        raise ProfileScopeError(
            "ir_binding_drift",
            "styled cue set does not match the timeline IR subtitle cues",
        )
    for cue in styled.cues:
        source = source_cues[cue.item_id]
        if cue.text != source.text or cue.lines != source.lines:
            raise ProfileScopeError(
                "text_changed",
                f"{cue.item_id}: profile application changed the cue text",
            )
        if cue.record_span != source.record_span:
            raise ProfileScopeError(
                "timing_changed",
                f"{cue.item_id}: profile application changed the cue timing",
            )


def verify_styled_presentation(
    styled: StyledPresentation,
    ir: TimelineIrProduction,
    *,
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    """Fail typed on any styled-vs-source drift; text/timing/style are invariants."""

    if styled.profile_snapshot_sha256 != profile.content_hash():
        raise ProfileScopeError(
            "profile_binding_drift",
            "styled output was produced under a different presentation profile",
        )
    if styled.registry_snapshot_sha256 != registry.content_hash():
        raise ProfileScopeError(
            "registry_binding_drift",
            "styled output was produced under a different asset registry",
        )
    verify_styled_ir_binding(styled, ir)
    for cue in styled.cues:
        if cue.style_id != profile.subtitle_style.style_id:
            raise ProfileScopeError(
                "style_id_changed",
                f"{cue.item_id}: cue style does not match the resolved profile",
            )


def _style_table(profile: ResolvedPresentationProfile) -> SubtitleStyleParams:
    return SubtitleStyleParams.model_validate(
        profile.subtitle_style.model_dump(mode="json")
    )


def apply_presentation_style(  # noqa: PLR0913 (brief-mandated styling contract)
    ir: TimelineIrProduction,
    *,
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    policy: SubtitleQcPolicy,
    titled: tuple[TitledItemRequest, ...],
    evidence: FactEvidenceContext,
    job_date: IsoDate,
    job_territory: TerritoryCode,
    artifact_id: str,
) -> StyledPresentation:
    """Deterministically style the compiled IR; fail-closed on any gate."""

    style = _style_table(profile)
    styled_cues = tuple(
        StyledCue(
            item_id=cue.item_id,
            text=cue.text,
            lines=cue.lines,
            record_span=cue.record_span,
            style_id=style.style_id,
            style=style,
            safe_area=cue.safe_area,
            min_duration_frames=cue.min_duration_frames,
        )
        for cue in _ir_cues(ir)
    )
    titled_items = tuple(
        build_titled_item(
            request,
            profile,
            style,
            registry,
            evidence,
            job_date=job_date,
            job_territory=job_territory,
        )
        for request in titled
    )
    draft = StyledPresentation(
        artifact_id=artifact_id,
        artifact_type="styled_presentation_v1",
        schema_version="styled-presentation-v1",
        content_hash=GENESIS_SHA256,
        producer=STYLE_PRODUCER,
        inputs=(),
        episode_id=profile.episode_id,
        profile_snapshot_sha256=profile.content_hash(),
        registry_snapshot_sha256=registry.content_hash(),
        ir_artifact_id=ir.artifact_id,
        ir_content_sha256=ir.content_hash,
        style_id=style.style_id,
        style=style,
        cues=styled_cues,
        titled_items=titled_items,
    )
    styled = draft.model_copy(update={"content_hash": artifact_content_hash(draft)})
    verify_styled_presentation(styled, ir, profile=profile, registry=registry)
    require_styled_qc(styled, policy)
    return styled


__all__ = [
    "STYLE_PRODUCER",
    "ProfileScopeError",
    "apply_presentation_style",
    "verify_styled_ir_binding",
    "verify_styled_presentation",
]
