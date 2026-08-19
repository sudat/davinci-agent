"""Presentation-styling models (Todo 57): re-exports of the contracts module.

The styled-presentation contract lives in
:mod:`services.contracts.styled_presentation` so the production Timeline IR
can embed it without an import cycle; this module keeps the historical
``services.presentation.styling_models`` import path stable for the
presentation, preview, and resolve-adapter consumers.
"""

from __future__ import annotations

from services.contracts.styled_presentation import (
    EpisodeMetadataEvidence,
    FactEvidenceContext,
    StyledCue,
    StyledPresentation,
    StyledQcRuleId,
    StyledQcViolation,
    StyledTitledItem,
    SubtitleStyleParams,
    TitledAnchor,
    TitledAssetRef,
    TitledAssetUsage,
    TitledEvidence,
    TitledItemRequest,
    TitledRole,
    TranscriptSpanEvidence,
    VerifiedMetadataEntry,
    VerifiedMetadataEvidence,
)

__all__ = [
    "EpisodeMetadataEvidence",
    "FactEvidenceContext",
    "StyledCue",
    "StyledPresentation",
    "StyledQcRuleId",
    "StyledQcViolation",
    "StyledTitledItem",
    "SubtitleStyleParams",
    "TitledAnchor",
    "TitledAssetRef",
    "TitledAssetUsage",
    "TitledEvidence",
    "TitledItemRequest",
    "TitledRole",
    "TranscriptSpanEvidence",
    "VerifiedMetadataEntry",
    "VerifiedMetadataEvidence",
]
