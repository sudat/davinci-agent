"""Todo-57 acceptance: profile-driven subtitle styles, overlays, and titles.

The SAME compiled production IR is styled under profile A and profile B:
cue text, wrapped lines, record spans, titled-item content, and the IR
editorial structure stay INVARIANT, and the only differences are the frozen
Todo-56 declared presentation dimensions (the 6 style parameters plus the
registry asset refs the output carries), asserted against the independent
golden table — never against implementation output. Every titled item is
evidence-gated, registry-resolved, deterministically placed, and QC'd
(Todo-44 rules recomputed + style/encoding/placement rules); a profile that
would change text or timing fails with a typed profile-scope error.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.compile.production_compiler import StylingInputs, compile_production
from services.compile.subtitle_policy import SubtitleQcPolicy
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.serialization import artifact_content_hash
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineIrProduction,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.fixtures.manifest import Phase0AFixtureManifest
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.foundation_io import canonical_model_bytes
from services.presentation.asset_registry import (
    RegistrySnapshot,
    register_assets,
    registry_from_phase3_manifests,
)
from services.presentation.models import (
    ChannelPresentationProfile,
    EpisodePresentationProfile,
    PlacementConfig,
)
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.styling import (
    ProfileScopeError,
    apply_presentation_style,
    verify_styled_presentation,
)
from services.presentation.styling_gate import UnsupportedFactError
from services.presentation.styling_models import (
    EpisodeMetadataEvidence,
    FactEvidenceContext,
    StyledPresentation,
    TitledItemRequest,
    TranscriptSpanEvidence,
    VerifiedMetadataEntry,
    VerifiedMetadataEvidence,
)
from services.presentation.styling_qc import (
    StyledQcError,
    require_styled_qc,
    run_styled_qc,
)
from services.preview.binding import initial_bindings, initial_timeline_ir
from services.preview.models import PreviewError
from services.preview.render import render_preview
from services.preview.tools import load_pinned_tools
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from tests.compile.support import (
    compile_fixture,
    generated_for,
    geometry_for,
    golden_transcript_cue_source,
)
from tests.editorial.support import GOLDEN_EXPECTED_PATH
from tests.presentation.test_manifest import _channel, _extra_entry, _system
from tests.resolve_adapter.support import (
    declared_media,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)

if TYPE_CHECKING:
    from services.presentation.models import ResolvedPresentationProfile
    from services.presentation.styling_models import (
        StyledCue,
        StyledTitledItem,
    )

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
GOLDEN_PATH = Path("tests/goldens/reference/phase-3/expected.json")
P0A_MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
PHASE_0C_LOCK = Path("config/toolchains/phase-0c-v1.json")
REF01 = "p1-ref-01-clean-ja"
SEGMENTS = ("s1", "s2", "s3", "s4")
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"
SEAL_LEAVES = frozenset(
    {"artifact_id", "content_hash", "profile_snapshot_sha256", "ir_artifact_id"}
)
STYLE_PARAM_KEYS = (
    "background_opacity_percent",
    "font_size_px",
    "margin_bottom_px",
    "outline_color_hex",
    "outline_width_px",
    "primary_color_hex",
)
RATE = RationalFrameRate(num=30, den=1)
MIRROR_PRODUCER = Producer(name="todo57-test", version="1")


def _policy() -> SubtitleQcPolicy:
    return SubtitleQcPolicy(
        policy_id="subtitle-qc-p3-v1",
        min_duration_frames=15,
        max_lines=2,
        max_chars_per_line=20,
        declared_style_refs=("style-default-ja", "style-presentation-default"),
        default_style_ref="style-default-ja",
    )


def _preview_policy() -> SubtitleQcPolicy:
    return SubtitleQcPolicy(
        policy_id="subtitle-qc-preview-v1",
        min_duration_frames=15,
        max_lines=2,
        max_chars_per_line=40,
        declared_style_refs=("style-default-ja", "style-presentation-default"),
        default_style_ref="style-default-ja",
    )


def _evidence_context() -> FactEvidenceContext:
    document: object = json.loads(GOLDEN_EXPECTED_PATH.read_bytes())
    assert isinstance(document, dict)
    spans = document["transcript_spans"][REF01]
    assert isinstance(spans, list)
    segments = {
        str(row["segment_id"]): str(row["text"])
        for row in spans
        if isinstance(row, dict) and str(row["text"])
    }
    return FactEvidenceContext(
        transcript_segments=segments,
        episode_metadata={"episode_title": "映像編集の基本"},
        verified_metadata={
            "location": VerifiedMetadataEntry(
                metadata_sha256="b" * 64, value="東京スタジオ"
            )
        },
    )


TITLED_REQUESTS: tuple[TitledItemRequest, ...] = (
    TitledItemRequest(
        item_id="title.chapter-1",
        role="chapter_title",
        text="基本を紹介",
        record_span=RecordFrameSpan(start_frame=0, end_frame=90),
        evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s1"),
    ),
    TitledItemRequest(
        item_id="title.chapter-2",
        role="chapter_title",
        text="映像編集の基本",
        record_span=RecordFrameSpan(start_frame=300, end_frame=360),
        evidence=EpisodeMetadataEvidence(kind="episode_metadata", key="episode_title"),
    ),
    TitledItemRequest(
        item_id="overlay.keyword-editing",
        role="keyword_overlay",
        text="映像編集",
        record_span=RecordFrameSpan(start_frame=150, end_frame=240),
        evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s1"),
    ),
)


def _apply(  # noqa: PLR0913, PLR0917 (styling contract fixed by the Todo-57 brief)
    ir: TimelineIrProduction,
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    artifact_id: str,
    titled: tuple[TitledItemRequest, ...] = TITLED_REQUESTS,
    evidence: FactEvidenceContext | None = None,
) -> StyledPresentation:
    return apply_presentation_style(
        ir,
        profile=profile,
        registry=registry,
        policy=_policy(),
        titled=titled,
        evidence=_evidence_context() if evidence is None else evidence,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        artifact_id=artifact_id,
    )


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-a.json").read_bytes()
    )


@pytest.fixture(scope="module")
def brand_b() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-b.json").read_bytes()
    )


@pytest.fixture(scope="module")
def golden() -> dict[str, object]:
    return json.loads(GOLDEN_PATH.read_bytes())


@pytest.fixture(scope="module")
def registry() -> RegistrySnapshot:
    return registry_from_phase3_manifests(MANIFEST_DIR)


@pytest.fixture(scope="module")
def catalog(registry: RegistrySnapshot) -> tuple[str, ...]:
    return tuple(sorted(entry.asset_id for entry in registry.entries))


def _profile_a(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot, catalog: tuple[str, ...]
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )


@pytest.fixture(scope="module")
def profile_a(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot, catalog: tuple[str, ...]
) -> ResolvedPresentationProfile:
    return _profile_a(brand_a, registry, catalog)


@pytest.fixture(scope="module")
def profile_b(
    brand_a: Phase3FixtureManifest,
    brand_b: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )


@pytest.fixture(scope="module")
def compiled_ir() -> TimelineIrProduction:
    return compile_fixture(
        REF01, transcript=golden_transcript_cue_source(REF01, SEGMENTS)
    ).ir


@pytest.fixture(scope="module")
def styled_a(
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> StyledPresentation:
    return _apply(compiled_ir, profile_a, registry, "styled-p3-a")


@pytest.fixture(scope="module")
def styled_b(
    compiled_ir: TimelineIrProduction,
    profile_b: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> StyledPresentation:
    return _apply(compiled_ir, profile_b, registry, "styled-p3-b")


def _leaf_key(path: str, usage: str | None) -> str:
    """Classify one leaf: a diff dimension, a seal, or a raw path."""

    leaf = path.rsplit(".", 1)[-1]
    if leaf in SEAL_LEAVES:
        return ""
    if leaf in STYLE_PARAM_KEYS:
        return f"style.{leaf}"
    if usage is not None and path.endswith((".asset.asset_id", ".asset.sha256")):
        return f"asset.{usage}.sha256"
    return f"path:{path}"


def _flatten_styled(payload: dict[str, object]) -> dict[str, list[object]]:
    """Map every leaf to its frozen diff dimension (or its raw path).

    Style-parameter leaves anywhere map to ``style.<param>``; titled-asset
    leaves map to ``asset.<usage>.sha256`` (id + content hash form one
    dimension group); seal/identity fields are skipped; every other leaf
    keeps its raw path so undeclared drift stays visible.
    """

    values: dict[str, list[object]] = {}

    def record(key: str, value: object) -> None:
        if key:
            values.setdefault(key, []).append(value)

    def walk(node: object, path: str, usage: str | None) -> None:
        if isinstance(node, dict):
            child_usage = usage
            if isinstance(node.get("usage"), str):
                child_usage = str(node["usage"])
            for key in sorted(node):
                walk(node[key], f"{path}.{key}" if path else str(key), child_usage)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", usage)
        else:
            record(_leaf_key(path, usage), node)

    walk(payload, "", None)
    return values


def _assert_ab_matches_frozen_golden(
    payload_a: dict[str, object],
    payload_b: dict[str, object],
    golden: dict[str, object],
    carried_usages: tuple[str, ...],
) -> None:
    ab_diff = golden["ab_diff"]
    assert isinstance(ab_diff, dict)
    declared_raw = ab_diff["declared_dimensions"]
    assert isinstance(declared_raw, list)
    declared = [str(dim) for dim in declared_raw]
    style_dims = {dim for dim in declared if dim.startswith("style.")}
    asset_dims = {dim for dim in declared if dim.startswith("asset.")}

    leaves_a = _flatten_styled(payload_a)
    leaves_b = _flatten_styled(payload_b)
    for dim, leaves in leaves_a.items():
        if dim.startswith("style."):
            assert len(set(map(json.dumps, leaves))) == 1, f"{dim} inconsistent in-payload"
    diffs = {
        key
        for key in set(leaves_a) | set(leaves_b)
        if leaves_a.get(key) != leaves_b.get(key)
    }
    carried = {f"style.{param}" for param in STYLE_PARAM_KEYS} | {
        f"asset.{usage}.sha256" for usage in carried_usages
    }
    assert carried <= style_dims | asset_dims
    assert diffs == carried, f"undeclared or missing presentation drift: {diffs ^ carried}"

    style_section = ab_diff["style"]
    assert isinstance(style_section, dict)
    for dim in sorted(style_dims):
        param = dim.partition(".")[2]
        entry = style_section[param]
        assert isinstance(entry, dict)
        assert entry["differ"] is True, dim
        assert leaves_a[f"style.{param}"][0] == entry["p3-brand-a"], dim
        assert leaves_b[f"style.{param}"][0] == entry["p3-brand-b"], dim
    asset_section = ab_diff["asset_sha256"]
    assert isinstance(asset_section, dict)
    for usage in carried_usages:
        entry = asset_section[usage]
        assert isinstance(entry, dict)
        assert entry["differ"] is True, usage
        assert entry["p3-brand-a"] in leaves_a[f"asset.{usage}.sha256"]
        assert entry["p3-brand-b"] in leaves_b[f"asset.{usage}.sha256"]
        assert (
            leaves_a[f"asset.{usage}.sha256"] != leaves_b[f"asset.{usage}.sha256"]
        )


def test_ab_styled_output_differs_only_in_the_frozen_declared_dimensions(
    styled_a: StyledPresentation,
    styled_b: StyledPresentation,
    golden: dict[str, object],
) -> None:
    _assert_ab_matches_frozen_golden(
        styled_a.model_dump(mode="json"),
        styled_b.model_dump(mode="json"),
        golden,
        carried_usages=("overlay", "logo"),
    )


def _cue_invariants(cue: StyledCue) -> tuple[object, ...]:
    return (
        cue.item_id,
        cue.text,
        cue.lines,
        cue.record_span,
        cue.safe_area,
        cue.min_duration_frames,
    )


def _titled_invariants(item: StyledTitledItem) -> tuple[object, ...]:
    return (
        item.item_id,
        item.role,
        item.text,
        item.record_span,
        item.anchor,
        item.safe_area_margin_px,
        item.evidence,
        item.evidence_text,
    )


def test_ab_text_timing_and_structure_are_invariant(
    styled_a: StyledPresentation, styled_b: StyledPresentation
) -> None:
    assert [_cue_invariants(cue) for cue in styled_a.cues] == [
        _cue_invariants(cue) for cue in styled_b.cues
    ]
    assert [_titled_invariants(item) for item in styled_a.titled_items] == [
        _titled_invariants(item) for item in styled_b.titled_items
    ]
    assert styled_a.episode_id == styled_b.episode_id
    assert styled_a.ir_artifact_id == styled_b.ir_artifact_id
    assert styled_a.ir_content_sha256 == styled_b.ir_content_sha256
    assert styled_a.registry_snapshot_sha256 == styled_b.registry_snapshot_sha256


def test_styled_cues_carry_the_ir_text_and_timing_verbatim(
    styled_a: StyledPresentation,
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
) -> None:
    ir_cues = {
        cue.item_id: cue
        for track in compiled_ir.tracks
        if track.track.kind == "subtitle"
        for cue in track.items
        if isinstance(cue, SubtitleCueItem)
    }
    assert ir_cues, "the compiled fixture IR carries subtitle cues"
    assert {cue.item_id for cue in styled_a.cues} == set(ir_cues)
    for cue in styled_a.cues:
        source = ir_cues[cue.item_id]
        assert cue.text == source.text
        assert cue.lines == source.lines
        assert cue.record_span == source.record_span
        assert cue.safe_area == source.safe_area
        assert cue.style_id == profile_a.subtitle_style.style_id


def test_titled_items_are_evidence_backed_registry_assets(
    styled_a: StyledPresentation,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    assert [item.item_id for item in styled_a.titled_items] == [
        request.item_id for request in TITLED_REQUESTS
    ]
    for item, request in zip(styled_a.titled_items, TITLED_REQUESTS, strict=True):
        assert item.evidence == request.evidence
        assert item.evidence_text
        usage = "overlay" if request.role == "keyword_overlay" else "logo"
        assert item.asset.usage == usage
        binding = next(b for b in profile_a.asset_bindings if b.kind == usage)
        entry = registry.entry_for(binding.asset_id)
        assert entry is not None
        assert item.asset.asset_id == entry.asset_id
        assert item.asset.sha256 == entry.sha256
        assert item.anchor == profile_a.placement.overlay_anchor == "top-left"
        assert item.safe_area_margin_px == profile_a.placement.safe_area_margin_px


def test_styled_qc_passes_for_both_brands(
    styled_a: StyledPresentation, styled_b: StyledPresentation
) -> None:
    require_styled_qc(styled_a, _policy())
    require_styled_qc(styled_b, _policy())
    assert run_styled_qc(styled_a, _policy()) == ()
    assert run_styled_qc(styled_b, _policy()) == ()


def test_double_apply_is_byte_identical(  # noqa: PLR0913, PLR0917 (one fixture per binding)
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    profile_b: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    styled_a: StyledPresentation,
    styled_b: StyledPresentation,
) -> None:
    again_a = _apply(compiled_ir, profile_a, registry, "styled-p3-a")
    again_b = _apply(compiled_ir, profile_b, registry, "styled-p3-b")
    for first, again in ((styled_a, again_a), (styled_b, again_b)):
        assert canonical_model_bytes(again) == canonical_model_bytes(first)
        assert again.content_hash == first.content_hash
        assert artifact_content_hash(again) == again.content_hash


def _wired_compile(
    profile: ResolvedPresentationProfile, registry: RegistrySnapshot, artifact_id: str
) -> TimelineIrProduction:
    return compile_production(
        generated_for(REF01),
        geometry_for(REF01),
        golden_transcript_cue_source(REF01, SEGMENTS),
        _policy(),
        artifact_id=artifact_id,
        styling=StylingInputs(
            profile=profile,
            registry=registry,
            titled=TITLED_REQUESTS,
            evidence=_evidence_context(),
            job_date=JOB_DATE,
            job_territory=JOB_TERRITORY,
        ),
    ).ir


def _subtitle_ids(ir: TimelineIrProduction) -> list[str]:
    return [
        cue.item_id
        for track in ir.tracks
        if track.track.kind == "subtitle"
        for cue in track.items
    ]


def test_compiler_emits_styled_presentation_into_the_ir(
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    wired = _wired_compile(profile_a, registry, "timeline-ir-p3-wired-a")
    assert wired.presentation is not None
    assert wired.tracks == compiled_ir.tracks
    assert wired.content_hash == compiled_ir.content_hash
    assert wired.presentation.style_id == profile_a.subtitle_style.style_id
    assert [cue.item_id for cue in wired.presentation.cues] == _subtitle_ids(compiled_ir)
    assert [item.item_id for item in wired.presentation.titled_items] == [
        request.item_id for request in TITLED_REQUESTS
    ]


def test_wired_ab_compiles_keep_the_editorial_structure_invariant(
    profile_a: ResolvedPresentationProfile,
    profile_b: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    golden: dict[str, object],
) -> None:
    wired_a = _wired_compile(profile_a, registry, "timeline-ir-p3-wired-a")
    wired_b = _wired_compile(profile_b, registry, "timeline-ir-p3-wired-b")
    assert wired_a.tracks == wired_b.tracks
    assert wired_a.content_hash == wired_b.content_hash
    assert wired_a.rate == wired_b.rate
    assert wired_a.presentation is not None
    assert wired_b.presentation is not None
    _assert_ab_matches_frozen_golden(
        wired_a.presentation.model_dump(mode="json"),
        wired_b.presentation.model_dump(mode="json"),
        golden,
        carried_usages=("overlay", "logo"),
    )


def test_profile_text_change_is_a_typed_failure(
    styled_a: StyledPresentation,
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    drifted_cue = styled_a.cues[0].model_copy(
        update={"text": styled_a.cues[0].text + "追記"}
    )
    drifted = styled_a.model_copy(update={"cues": (drifted_cue, *styled_a.cues[1:])})
    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(drifted, compiled_ir, profile=profile_a, registry=registry)
    assert excinfo.value.reason == "text_changed"

    # Adversarial: re-sealing the tampered table must NOT launder the drift.
    resealed = drifted.model_copy(update={"content_hash": artifact_content_hash(drifted)})
    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(resealed, compiled_ir, profile=profile_a, registry=registry)
    assert excinfo.value.reason == "text_changed"


def test_profile_timing_change_is_a_typed_failure(
    styled_a: StyledPresentation,
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    span = styled_a.cues[0].record_span
    drifted_cue = styled_a.cues[0].model_copy(
        update={
            "record_span": RecordFrameSpan(
                start_frame=span.start_frame + 1, end_frame=span.end_frame
            )
        }
    )
    drifted = styled_a.model_copy(update={"cues": (drifted_cue, *styled_a.cues[1:])})
    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(drifted, compiled_ir, profile=profile_a, registry=registry)
    assert excinfo.value.reason == "timing_changed"


def test_factual_overlay_without_evidence_is_a_typed_failure(
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    missing = TitledItemRequest(
        item_id="overlay.hallucinated",
        role="keyword_overlay",
        text="視聴者は100万人",
        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
        evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s-missing"),
    )
    with pytest.raises(UnsupportedFactError) as excinfo:
        _apply(compiled_ir, profile_a, registry, "styled-bad-evidence", titled=(missing,))
    assert excinfo.value.reason == "missing_evidence"

    uncovered = TitledItemRequest(
        item_id="overlay.invented-number",
        role="keyword_overlay",
        text="視聴者は100万人",
        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
        evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s1"),
    )
    with pytest.raises(UnsupportedFactError) as excinfo:
        _apply(compiled_ir, profile_a, registry, "styled-bad-evidence", titled=(uncovered,))
    assert excinfo.value.reason == "evidence_mismatch"

    stale_hash = TitledItemRequest(
        item_id="overlay.studio",
        role="keyword_overlay",
        text="東京スタジオ",
        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
        evidence=VerifiedMetadataEvidence(
            kind="verified_metadata", metadata_sha256="c" * 64, key="location"
        ),
    )
    with pytest.raises(UnsupportedFactError) as excinfo:
        _apply(compiled_ir, profile_a, registry, "styled-bad-evidence", titled=(stale_hash,))
    assert excinfo.value.reason == "evidence_mismatch"


def test_unsafe_and_overlapping_placement_fail_typed(
    compiled_ir: TimelineIrProduction,
    brand_a: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> None:
    profile = _profile_a(brand_a, registry, catalog)
    clashing = (
        TitledItemRequest(
            item_id="overlay.kw-1",
            role="keyword_overlay",
            text="映像編集",
            record_span=RecordFrameSpan(start_frame=0, end_frame=120),
            evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s1"),
        ),
        TitledItemRequest(
            item_id="overlay.kw-2",
            role="keyword_overlay",
            text="基本",
            record_span=RecordFrameSpan(start_frame=60, end_frame=180),
            evidence=TranscriptSpanEvidence(kind="transcript_span", segment_id="s1"),
        ),
    )
    with pytest.raises(StyledQcError) as excinfo:
        _apply(compiled_ir, profile, registry, "styled-overlap", titled=clashing)
    assert "titled_item_overlap" in {v.rule_id for v in excinfo.value.violations}

    rogue_channel = ChannelPresentationProfile.model_validate(
        {
            "channel_id": "channel-unsafe-anchor",
            "placement": PlacementConfig(
                intro_duration_frames=30,
                outro_duration_frames=30,
                logo_anchor="bottom-right",
                overlay_anchor="bottom-left",
                safe_area_margin_px=48,
            ),
        }
    )
    rogue_profile = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=rogue_channel,
        registry=registry,
    )
    with pytest.raises(StyledQcError) as excinfo:
        _apply(compiled_ir, rogue_profile, registry, "styled-unsafe-anchor")
    assert "titled_placement_collision" in {v.rule_id for v in excinfo.value.violations}


def test_encoding_and_style_rules_fail_typed(styled_a: StyledPresentation) -> None:
    bad_encoding = styled_a.cues[0].model_copy(update={"text": "不正な\x00文字"})
    rogue_style_id = styled_a.cues[1].model_copy(update={"style_id": "style-rogue"})
    out_of_range = styled_a.cues[2].model_copy(
        update={"style": styled_a.cues[2].style.model_copy(update={"font_size_px": 999})}
    )
    tampered = styled_a.model_copy(
        update={"cues": (bad_encoding, rogue_style_id, out_of_range, *styled_a.cues[3:])}
    )
    rules = {v.rule_id for v in run_styled_qc(tampered, _policy())}
    assert {"text_encoding", "style_unregistered", "style_param_out_of_range"} <= rules
    with pytest.raises(StyledQcError):
        require_styled_qc(tampered, _policy())

    table_drift = styled_a.model_copy(
        update={"style": styled_a.style.model_copy(update={"font_size_px": 999})}
    )
    rules = {v.rule_id for v in run_styled_qc(table_drift, _policy())}
    assert "style_param_out_of_range" in rules


def test_binding_and_evidence_drift_block_typed(
    styled_a: StyledPresentation,
    compiled_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    profile_b: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> None:
    drifted_registry = register_assets(*registry.entries, _extra_entry())
    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(
            styled_a, compiled_ir, profile=profile_a, registry=drifted_registry
        )
    assert excinfo.value.reason == "registry_binding_drift"

    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(styled_a, compiled_ir, profile=profile_b, registry=registry)
    assert excinfo.value.reason == "profile_binding_drift"

    other_ir = compiled_ir.model_copy(update={"artifact_id": "timeline-ir-rebound"})
    with pytest.raises(ProfileScopeError) as excinfo:
        verify_styled_presentation(styled_a, other_ir, profile=profile_a, registry=registry)
    assert excinfo.value.reason == "ir_binding_drift"

    with pytest.raises(UnsupportedFactError) as excinfo:
        _apply(
            compiled_ir,
            profile_a,
            registry,
            "styled-stale-evidence",
            evidence=FactEvidenceContext(),
        )
    assert excinfo.value.reason == "missing_evidence"


def test_malformed_styled_payloads_fail_closed(styled_a: StyledPresentation) -> None:
    payload = json.loads(styled_a.model_dump_json())

    unknown_field = dict(payload, mystery_key="x")
    with pytest.raises(ValidationError):
        StyledPresentation.model_validate_json(json.dumps(unknown_field))

    resolve_field = dict(payload, resolve_track_index=1)
    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        StyledPresentation.model_validate_json(json.dumps(resolve_field))

    bad_hash = dict(payload, ir_content_sha256="not-a-hash")
    with pytest.raises(ValidationError):
        StyledPresentation.model_validate_json(json.dumps(bad_hash))

    out_of_range_style = dict(payload, style=dict(payload["style"], font_size_px=9999))
    with pytest.raises(ValidationError):
        StyledPresentation.model_validate_json(json.dumps(out_of_range_style))

    assert StyledPresentation.model_validate(styled_a.model_dump()) == styled_a


def test_resolve_package_carries_the_styled_presentation(
    profile_a: ResolvedPresentationProfile, registry: RegistrySnapshot
) -> None:
    manifest = load_p2_manifest("p2-blocking-qc-privacy")
    ir = ir_for(manifest)
    styled = apply_presentation_style(
        ir,
        profile=profile_a,
        registry=registry,
        policy=_policy(),
        titled=(),
        evidence=FactEvidenceContext(),
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        artifact_id="styled-p2",
    )
    request = PackageCompileRequest(
        ir=ir,
        lock=phase2_lock(),
        lock_sha256=lock_sha256(),
        declared_media=declared_media(manifest),
        artifact_id="resolve-package-styled",
        styled_presentation=styled,
    )
    package = compile_resolve_package(request)
    assert package.styled_presentation == styled
    assert package.subtitle_step is not None
    assert {cue.item_id for cue in styled.cues} == {
        cue.cue_id for cue in package.subtitle_step.cues
    }
    assert all(cue.style_id == "style-presentation-default" for cue in styled.cues)

    drifted = styled.model_copy(update={"cues": ()})
    with pytest.raises(PackageCompileError) as excinfo:
        compile_resolve_package(
            PackageCompileRequest(
                ir=ir,
                lock=phase2_lock(),
                lock_sha256=lock_sha256(),
                declared_media=declared_media(manifest),
                artifact_id="resolve-package-styled-drift",
                styled_presentation=drifted,
            )
        )
    assert excinfo.value.code == "styled-presentation-drift"


def test_preview_records_the_style_table_soft_sub_only(
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    tmp_path: Path,
) -> None:
    try:
        tools = load_pinned_tools(PHASE_0C_LOCK)
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")
    fixture_dir = tools.ffmpeg.parents[3] / "phase-0a" / "fixture"
    if not (fixture_dir / "source.mov").is_file():
        pytest.skip(f"phase 0A fixture media not materialized: {fixture_dir}")
    manifest = Phase0AFixtureManifest.model_validate_json(P0A_MANIFEST.read_bytes())
    ir0c = initial_timeline_ir(manifest)
    bindings = initial_bindings(fixture_dir)
    recipe = manifest.recipe.subtitle

    draft = TimelineIrProduction(
        artifact_id="timeline-ir-preview-mirror",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash="0" * 64,
        producer=MIRROR_PRODUCER,
        inputs=(),
        rate=RATE,
        tracks=(
            TimelineTrackProduction(
                track=TrackRef0C(kind="subtitle", index=3),
                items=(
                    SubtitleCueItem(
                        item_id="cue.preview-mirror-1",
                        source=SourceRef(
                            source_id="preview-mirror-source",
                            span=SourceFrameSpan(
                                start_frame=recipe.record_span.start_frame,
                                end_frame=recipe.record_span.end_frame,
                                rate=RATE,
                            ),
                        ),
                        record_span=RecordFrameSpan(
                            start_frame=recipe.record_span.start_frame,
                            end_frame=recipe.record_span.end_frame,
                        ),
                        text=recipe.text,
                        lines=(recipe.text,),
                        style_ref="style-default-ja",
                        safe_area=True,
                        min_duration_frames=15,
                    ),
                ),
            ),
        ),
    )
    mirror = draft.model_copy(update={"content_hash": artifact_content_hash(draft)})
    styled = apply_presentation_style(
        mirror,
        profile=profile_a,
        registry=registry,
        policy=_preview_policy(),
        titled=(),
        evidence=FactEvidenceContext(),
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        artifact_id="styled-preview",
    )

    trace_plain = render_preview(None, ir0c, bindings, tmp_path / "plain", tools=tools)
    trace_styled = render_preview(
        None, ir0c, bindings, tmp_path / "styled", tools=tools, styled=styled
    )
    assert trace_plain.presentation_style is None
    table = trace_styled.presentation_style
    assert table is not None
    assert table.style_id == profile_a.subtitle_style.style_id
    assert table.params.model_dump() == profile_a.subtitle_style.model_dump()
    assert table.cue_style_ids == (profile_a.subtitle_style.style_id,)
    assert table.titled_item_ids == ()
    assert table.styled_presentation_sha256 == hashlib.sha256(
        canonical_model_bytes(styled)
    ).hexdigest()
    assert trace_styled.ffprobe_summary.subtitle_codec == "mov_text"
    assert (
        trace_styled.preview.decoded_video_sha256
        == trace_plain.preview.decoded_video_sha256
    )
