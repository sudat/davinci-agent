"""ColorFinishingPlanV1 — color finishing as a first-class plan (task 35).

PRD 10.4 / implementation plan 8.5: color finishing is expressed as
SEPARATE typed sections — technical correction (exposure / white
balance), camera & shot matching, channel/episode look, reference
sanity checks where relevant, and render-side visual QC. The plan says
WHAT correction/matching/look is wanted, never HOW a specific NLE
applies it (ResolveFreeModel rejects ``resolve*`` keys; live MCP
grading is chosen via ``preferred_path`` metadata, not by embedding
API calls).

Justified no-op contract (task 35 core): every section with
``needed: false`` MUST carry a non-blank ``justification`` — an empty
plan without justifications is unrepresentable (typed
``noop_justification_required`` rejection). Symmetrically, an enabled
section must cite evidence, ``justification`` is only for no-ops, a
matching no-op carries no groups, and a look no-op carries no
``look_ref``. ``build_color_plan`` derives sections deterministically
from :class:`ColorFactsV1` and auto-writes builder justifications for
no-ops, so hand-built or LLM-proposed plans are held to the same
contract the builder satisfies.

Preferred path (pure function of the mcp-fit color rows' statuses —
``capabilities/v4.3/mcp-fit.json`` rows ``color-grade-preset-drx`` and
``advanced-delivery-qc``):

    color-grade-preset-drx accepted            -> mcp_live_grading
    else advanced-delivery-qc accepted         -> advanced_drx_qc
    else                                       -> external

Live MCP grading is the preferred path where fixtures pass; a per-shot
generative grade is NOT required (stable channel looks, matching, and
readable images over random stylistic variation).

Tuple coercion: every ``tuple`` field carries ``BeforeValidator(_to_tuple)``
so JSON lists round-trip (cf. tasks 12/22/24/31).
"""

# allow: SIZE_OK — task 35 pins this deliverable to the single-module commit
# scope (color_finishing.py); 176 pure code lines, the remainder is the
# contract docstring (no-op semantics + preferred-path table). T31/47 SIZE_OK
# precedent for plan-pinned single service files.

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    ResolveFreeModel,
    SourceId,
    StrictModel,
)

# Mirrors services.toolchain.mcp_fit.VALID_STATUSES; kept as a static
# Literal so capability statuses parse into the type system at boundary.
McpCapabilityStatus = Literal["accepted", "failed", "partial", "not_available"]

PreferredPath = Literal["mcp_live_grading", "advanced_drx_qc", "external"]

_MATCHING_MIN_CAMERAS: Final[int] = 2


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


_StrTuple = Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]


# ------------------------------------------------------------ color facts


class ColorIssueV1(ResolveFreeModel):
    """One observed technical color issue on one source."""

    source_id: SourceId
    detail: Annotated[str, Field(min_length=1, strict=True)]


class CameraSourceV1(ResolveFreeModel):
    """A camera identity plus the sources it shot (matching input)."""

    camera_id: Identifier
    source_ids: Annotated[tuple[SourceId, ...], BeforeValidator(_to_tuple)] = ()


class ColorFactsV1(ResolveFreeModel):
    """Analysis facts the plan is derived from (observed reality)."""

    episode_id: Identifier
    exposure_issues: Annotated[tuple[ColorIssueV1, ...], BeforeValidator(_to_tuple)] = ()
    wb_issues: Annotated[tuple[ColorIssueV1, ...], BeforeValidator(_to_tuple)] = ()
    cameras: Annotated[tuple[CameraSourceV1, ...], BeforeValidator(_to_tuple)] = ()
    look_configured: bool = False
    look_ref: Identifier | None = None
    skin_tone_relevant: bool = False
    product_relevant: bool = False


# --------------------------------------------------------------- sections


class _JustifiedSection(ResolveFreeModel):
    """Shared contract: no-ops are justified, enabled sections cite evidence."""

    needed: bool
    evidence: _StrTuple = ()
    justification: str | None = None

    @model_validator(mode="after")
    def _enforce_justification_contract(self) -> _JustifiedSection:
        if self.needed:
            if not self.evidence:
                raise PydanticCustomError(
                    "needed_requires_evidence",
                    "an enabled section must cite evidence",
                )
            if self.justification is not None:
                raise PydanticCustomError(
                    "justification_only_for_noop",
                    "justification is only meaningful on a no-op section",
                )
        elif not (self.justification and self.justification.strip()):
            raise PydanticCustomError(
                "noop_justification_required",
                "a no-op section requires an explicit justification",
            )
        return self


class CorrectionItemV1(_JustifiedSection):
    """Exposure or white-balance technical correction item."""


class MatchGroupV1(ResolveFreeModel):
    """Cameras/shots that must be matched to one another."""

    group_id: Identifier
    camera_ids: Annotated[
        tuple[Identifier, ...], Field(min_length=1), BeforeValidator(_to_tuple)
    ] = ()
    note: str = ""


class CameraShotMatchingV1(_JustifiedSection):
    """Camera-to-camera and shot-to-shot matching."""

    groups: Annotated[tuple[MatchGroupV1, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def _enforce_group_parity(self) -> CameraShotMatchingV1:
        if self.needed and not self.groups:
            raise PydanticCustomError(
                "matching_requires_groups",
                "enabled matching must define at least one match group",
            )
        if not self.needed and self.groups:
            raise PydanticCustomError(
                "matching_noop_has_groups",
                "a matching no-op must not define match groups",
            )
        group_ids = [group.group_id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise PydanticCustomError("duplicate_group_id", "match group ids are unique")
        return self


class ChannelEpisodeLookV1(_JustifiedSection):
    """Channel/episode look (look_ref None = channel default, cf. task 31)."""

    look_ref: Identifier | None = None

    @model_validator(mode="after")
    def _enforce_look_ref_parity(self) -> ChannelEpisodeLookV1:
        if not self.needed and self.look_ref is not None:
            raise PydanticCustomError(
                "noop_has_look_ref",
                "a look no-op must not reference a look",
            )
        return self


class TechnicalCorrectionV1(ResolveFreeModel):
    """Technical correction: exposure and white balance are separate items."""

    exposure: CorrectionItemV1
    white_balance: CorrectionItemV1


class ReferenceSanityCheckV1(ResolveFreeModel):
    """Skin/product/reference sanity check, present only where relevant."""

    check_id: Identifier
    kind: Literal["skin_tone", "product", "reference"]
    note: str = ""


class VisualQcCheckV1(ResolveFreeModel):
    """Render-side visual QC check, delegated/referenced (never executed here)."""

    check_id: Identifier
    delegated_to: Identifier
    note: str = ""


# ----------------------------------------------------------- plan + policy


class ColorPlanPolicy(StrictModel):
    """Plan-steering inputs: capability statuses + QC delegation."""

    color_grade_status: McpCapabilityStatus
    advanced_qc_status: McpCapabilityStatus
    visual_qc_checks: Annotated[tuple[VisualQcCheckV1, ...], BeforeValidator(_to_tuple)] = ()


class ColorFinishingPlanV1(ResolveFreeModel):
    schema_version: Literal["color-finishing-plan-v1"]
    episode_id: Identifier
    technical_correction: TechnicalCorrectionV1
    camera_shot_matching: CameraShotMatchingV1
    channel_episode_look: ChannelEpisodeLookV1
    reference_sanity_checks: Annotated[
        tuple[ReferenceSanityCheckV1, ...], BeforeValidator(_to_tuple)
    ] = ()
    visual_qc: Annotated[tuple[VisualQcCheckV1, ...], BeforeValidator(_to_tuple)] = ()
    preferred_path: PreferredPath

    @model_validator(mode="after")
    def _require_unique_check_ids(self) -> ColorFinishingPlanV1:
        sanity_ids = [check.check_id for check in self.reference_sanity_checks]
        if len(set(sanity_ids)) != len(sanity_ids):
            raise PydanticCustomError("duplicate_check_id", "reference sanity check ids are unique")
        qc_ids = [check.check_id for check in self.visual_qc]
        if len(set(qc_ids)) != len(qc_ids):
            raise PydanticCustomError("duplicate_check_id", "visual qc check ids are unique")
        return self


# -------------------------------------------------------- preferred path


def preferred_path_for(
    color_grade_status: McpCapabilityStatus,
    advanced_qc_status: McpCapabilityStatus,
) -> PreferredPath:
    """Pure function of the mcp-fit color rows' statuses (table above)."""
    if color_grade_status == "accepted":
        return "mcp_live_grading"
    if advanced_qc_status == "accepted":
        return "advanced_drx_qc"
    return "external"


# ------------------------------------------------------------- builder


def _issue_evidence(issues: tuple[ColorIssueV1, ...]) -> tuple[str, ...]:
    return tuple(f"{issue.source_id}: {issue.detail}" for issue in issues)


def build_color_plan(color_facts: ColorFactsV1, *, policy: ColorPlanPolicy) -> ColorFinishingPlanV1:
    """Derive a justified color plan from analysis facts (deterministic, pure)."""
    cameras = sorted({camera.camera_id for camera in color_facts.cameras})
    multi_camera = len(cameras) >= _MATCHING_MIN_CAMERAS

    matching = (
        CameraShotMatchingV1(
            needed=True,
            evidence=tuple(f"camera:{camera_id}" for camera_id in cameras),
            groups=(MatchGroupV1(group_id="match-group-1", camera_ids=tuple(cameras)),),
        )
        if multi_camera
        else CameraShotMatchingV1(
            needed=False,
            justification=(
                f"single camera ({cameras[0]}); no cross-camera matching required"
                if cameras
                else "no camera sources recorded; matching not applicable"
            ),
        )
    )

    look = (
        ChannelEpisodeLookV1(
            needed=True,
            evidence=(
                f"look {color_facts.look_ref} configured"
                if color_facts.look_ref is not None
                else "channel default look configured",
            ),
            look_ref=color_facts.look_ref,
        )
        if color_facts.look_configured
        else ChannelEpisodeLookV1(
            needed=False, justification="no channel or episode look configured"
        )
    )

    reference_checks: tuple[ReferenceSanityCheckV1, ...] = (
        (ReferenceSanityCheckV1(check_id="chk-skin-tone", kind="skin_tone"),)
        if color_facts.skin_tone_relevant
        else ()
    )
    if color_facts.product_relevant:
        reference_checks = (
            *reference_checks,
            ReferenceSanityCheckV1(check_id="chk-product", kind="product"),
        )

    return ColorFinishingPlanV1(
        schema_version="color-finishing-plan-v1",
        episode_id=color_facts.episode_id,
        technical_correction=TechnicalCorrectionV1(
            exposure=CorrectionItemV1(
                needed=bool(color_facts.exposure_issues),
                evidence=_issue_evidence(color_facts.exposure_issues),
                justification=None
                if color_facts.exposure_issues
                else "analysis reported no exposure issues",
            ),
            white_balance=CorrectionItemV1(
                needed=bool(color_facts.wb_issues),
                evidence=_issue_evidence(color_facts.wb_issues),
                justification=None
                if color_facts.wb_issues
                else "analysis reported no white-balance issues",
            ),
        ),
        camera_shot_matching=matching,
        channel_episode_look=look,
        reference_sanity_checks=reference_checks,
        visual_qc=policy.visual_qc_checks,
        preferred_path=preferred_path_for(policy.color_grade_status, policy.advanced_qc_status),
    )


__all__ = [
    "CameraShotMatchingV1",
    "CameraSourceV1",
    "ChannelEpisodeLookV1",
    "ColorFactsV1",
    "ColorFinishingPlanV1",
    "ColorIssueV1",
    "ColorPlanPolicy",
    "CorrectionItemV1",
    "MatchGroupV1",
    "McpCapabilityStatus",
    "PreferredPath",
    "ReferenceSanityCheckV1",
    "TechnicalCorrectionV1",
    "VisualQcCheckV1",
    "build_color_plan",
    "preferred_path_for",
]
