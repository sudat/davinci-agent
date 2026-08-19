"""The styled-presentation contract (Todo 57, PRD 19.2-19.3): NLE-neutral.

Subtitle cues compiled under the frozen Todo-44 rules are styled by the
resolved profile ONLY in appearance: text, wrapped lines, and record spans
are carried verbatim, and any drift between a styled table and its source
IR is a typed profile-scope failure. Keyword overlays and chapter titles
are titled items: every text must be backed by declared fact evidence
(transcript span, episode metadata, or hash-verified metadata), resolve a
registry asset per profile binding, and place deterministically from the
declared record spans. This module lives in ``services.contracts`` so the
production Timeline IR can embed it without a package cycle; it depends on
nothing outside the contracts package.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StringConstraints

from services.contracts.primitives import (
    Identifier,
    ItemId,
    RecordFrameSpan,
    ResolveFreeEnvelope,
    ResolveFreeModel,
    Sha256,
)

type TitledRole = Literal["keyword_overlay", "chapter_title"]
type TitledAnchor = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
type TitledAssetUsage = Literal["intro", "logo", "outro", "overlay", "se", "tone"]
type StyleHexColor = Annotated[
    str, StringConstraints(pattern=r"^#[0-9A-F]{6}$", strict=True)
]


def _tuple[Value](value: list[Value] | tuple[Value, ...]) -> tuple[Value, ...]:
    return tuple(value)


type StyleSequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(_tuple)]


class SubtitleStyleParams(ResolveFreeModel):
    """Resolved subtitle style parameters; text content never lives in a style."""

    style_id: Identifier
    font_family: str = Field(min_length=1)
    font_size_px: int = Field(ge=8, le=200, strict=True)
    primary_color_hex: StyleHexColor
    outline_color_hex: StyleHexColor
    outline_width_px: int = Field(ge=0, le=12, strict=True)
    margin_bottom_px: int = Field(ge=0, le=480, strict=True)
    background_opacity_percent: int = Field(ge=0, le=100, strict=True)


class TranscriptSpanEvidence(ResolveFreeModel):
    """Fact evidence: one declared transcript cue segment."""

    kind: Literal["transcript_span"]
    segment_id: Identifier


class EpisodeMetadataEvidence(ResolveFreeModel):
    """Fact evidence: one declared episode-metadata key."""

    kind: Literal["episode_metadata"]
    key: str = Field(min_length=1, strict=True)


class VerifiedMetadataEvidence(ResolveFreeModel):
    """Fact evidence: one metadata entry fixed by its verified content hash."""

    kind: Literal["verified_metadata"]
    metadata_sha256: Sha256
    key: str = Field(min_length=1, strict=True)


type TitledEvidence = Annotated[
    TranscriptSpanEvidence | EpisodeMetadataEvidence | VerifiedMetadataEvidence,
    Field(discriminator="kind"),
]


class TitledItemRequest(ResolveFreeModel):
    """A declared titled item: text + record span + a mandatory evidence ref."""

    item_id: Identifier
    role: TitledRole
    text: str = Field(min_length=1, strict=True)
    record_span: RecordFrameSpan
    evidence: TitledEvidence


class VerifiedMetadataEntry(ResolveFreeModel):
    """One verified-metadata entry: content hash plus the declared value."""

    metadata_sha256: Sha256
    value: str = Field(min_length=1, strict=True)


class FactEvidenceContext(ResolveFreeModel):
    """The declared fact-evidence sources; lookups are exact, never fuzzy."""

    transcript_segments: dict[Identifier, str] = Field(default_factory=dict)
    episode_metadata: dict[str, str] = Field(default_factory=dict)
    verified_metadata: dict[str, VerifiedMetadataEntry] = Field(default_factory=dict)


class StyledCue(ResolveFreeModel):
    """A subtitle cue with profile styling; text/timing are verbatim copies."""

    kind: Literal["styled_cue"] = "styled_cue"
    item_id: ItemId
    text: str = Field(min_length=1, strict=True)
    lines: StyleSequence[str] = Field(min_length=1)
    record_span: RecordFrameSpan
    style_id: Identifier
    style: SubtitleStyleParams
    safe_area: bool
    min_duration_frames: int = Field(gt=0, strict=True)


class TitledAssetRef(ResolveFreeModel):
    """A registry-resolved presentation asset fixed by content hash."""

    asset_id: Identifier
    sha256: Sha256
    usage: TitledAssetUsage


class StyledTitledItem(ResolveFreeModel):
    """One evidence-backed keyword overlay or chapter title, styled + placed."""

    kind: Literal["styled_titled_item"] = "styled_titled_item"
    item_id: Identifier
    role: TitledRole
    text: str = Field(min_length=1, strict=True)
    record_span: RecordFrameSpan
    anchor: TitledAnchor
    safe_area_margin_px: int = Field(ge=0, strict=True)
    style_id: Identifier
    style: SubtitleStyleParams
    asset: TitledAssetRef
    evidence: TitledEvidence
    evidence_text: str = Field(min_length=1, strict=True)


class StyledPresentation(ResolveFreeEnvelope[Literal["styled_presentation_v1"]]):
    """The sealed styled-presentation artifact bound to one production IR.

    The envelope records the profile/registry snapshot hashes and the source
    IR binding; the editorial content (cue text, lines, record spans, titled
    texts, evidence) is invariant across profiles by construction.
    """

    episode_id: Identifier
    profile_snapshot_sha256: Sha256
    registry_snapshot_sha256: Sha256
    ir_artifact_id: Identifier
    ir_content_sha256: Sha256
    style_id: Identifier
    style: SubtitleStyleParams
    cues: StyleSequence[StyledCue] = ()
    titled_items: StyleSequence[StyledTitledItem] = ()


StyledQcRuleId = Literal[
    "subtitle_min_duration",
    "subtitle_max_lines",
    "subtitle_max_chars",
    "subtitle_safe_area",
    "subtitle_cue_overlap",
    "style_unregistered",
    "style_table_mismatch",
    "style_param_out_of_range",
    "text_encoding",
    "titled_placement_collision",
    "titled_item_overlap",
    "fact_without_evidence",
]


class StyledQcViolation(ResolveFreeModel):
    """One typed styled-QC rule violation bound to the offending item."""

    rule_id: StyledQcRuleId
    item_id: str
    detail: str


__all__ = [
    "EpisodeMetadataEvidence",
    "FactEvidenceContext",
    "StyleHexColor",
    "StyleSequence",
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
