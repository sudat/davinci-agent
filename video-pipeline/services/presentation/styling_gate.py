"""The fact gate and titled-item assembly (Todo 57, PRD 19.3).

An overlay/title text is a FACT claim: it leaves the gate only when every
whitespace token of the text is covered by declared evidence — a transcript
span, an episode-metadata value, or a hash-verified metadata entry — and the
item's presentation asset resolves through the profile's registry binding
under the rights gate. Anything else is a typed ``unsupported-fact``
failure; the AI never completes numbers or proper nouns on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from services.presentation.asset_registry import AssetUsage, check_rights
from services.presentation.manifest import ManifestError
from services.presentation.styling_models import (
    EpisodeMetadataEvidence,
    StyledTitledItem,
    TitledAssetRef,
    TitledItemRequest,
    TitledRole,
    TranscriptSpanEvidence,
    VerifiedMetadataEvidence,
)

if TYPE_CHECKING:
    from services.contracts.styled_presentation import SubtitleStyleParams
    from services.presentation.asset_registry import (
        IsoDate,
        RegistrySnapshot,
        TerritoryCode,
    )
    from services.presentation.models import AssetBinding, ResolvedPresentationProfile
    from services.presentation.styling_models import (
        FactEvidenceContext,
        TitledEvidence,
    )

TITLED_ASSET_USAGE: Final[dict[TitledRole, AssetUsage]] = {
    "keyword_overlay": "overlay",
    "chapter_title": "logo",
}

type FactGateReason = Literal["missing_evidence", "evidence_mismatch"]


class UnsupportedFactError(ValueError):
    """A titled item text lacks declared, covering fact evidence."""

    def __init__(self, reason: FactGateReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _supporting_text(evidence: TitledEvidence, context: FactEvidenceContext) -> str:
    if isinstance(evidence, TranscriptSpanEvidence):
        text = context.transcript_segments.get(evidence.segment_id)
        if text is None:
            raise UnsupportedFactError(
                "missing_evidence",
                f"transcript segment {evidence.segment_id} is not declared evidence",
            )
        return text
    if isinstance(evidence, EpisodeMetadataEvidence):
        value = context.episode_metadata.get(evidence.key)
        if value is None:
            raise UnsupportedFactError(
                "missing_evidence",
                f"episode metadata key {evidence.key!r} is not declared evidence",
            )
        return value
    if isinstance(evidence, VerifiedMetadataEvidence):
        entry = context.verified_metadata.get(evidence.key)
        if entry is None:
            raise UnsupportedFactError(
                "missing_evidence",
                f"verified metadata key {evidence.key!r} is not declared evidence",
            )
        if entry.metadata_sha256 != evidence.metadata_sha256:
            raise UnsupportedFactError(
                "evidence_mismatch",
                f"verified metadata {evidence.key!r} hash drifted from the declaration",
            )
        return entry.value
    raise UnsupportedFactError("missing_evidence", "unknown evidence kind")


def _tokens_supported(text: str, support: str) -> bool:
    tokens = text.split()
    return bool(tokens) and all(token in support for token in tokens)


def _binding_for(profile: ResolvedPresentationProfile, usage: AssetUsage) -> AssetBinding:
    for binding in profile.asset_bindings:
        if binding.kind == usage:
            return binding
    raise ManifestError(f"resolved profile carries no binding for usage {usage}")


def build_titled_item(  # noqa: PLR0913 (rights gate needs the full job context)
    request: TitledItemRequest,
    profile: ResolvedPresentationProfile,
    style: SubtitleStyleParams,
    registry: RegistrySnapshot,
    context: FactEvidenceContext,
    *,
    job_date: IsoDate,
    job_territory: TerritoryCode,
) -> StyledTitledItem:
    """Gate the fact, resolve the registry asset, and place deterministically."""

    support = _supporting_text(request.evidence, context)
    if not _tokens_supported(request.text, support):
        raise UnsupportedFactError(
            "evidence_mismatch",
            f"{request.item_id}: text tokens are not covered by the declared evidence",
        )
    usage = TITLED_ASSET_USAGE[request.role]
    binding = _binding_for(profile, usage)
    entry = check_rights(
        registry,
        binding.asset_id,
        usage=usage,
        territory=job_territory,
        at_job=job_date,
    )
    return StyledTitledItem(
        item_id=request.item_id,
        role=request.role,
        text=request.text,
        record_span=request.record_span,
        anchor=profile.placement.overlay_anchor,
        safe_area_margin_px=profile.placement.safe_area_margin_px,
        style_id=style.style_id,
        style=style,
        asset=TitledAssetRef(asset_id=entry.asset_id, sha256=entry.sha256, usage=entry.usage),
        evidence=request.evidence,
        evidence_text=support,
    )


__all__ = [
    "TITLED_ASSET_USAGE",
    "UnsupportedFactError",
    "build_titled_item",
]
