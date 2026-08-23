"""Task 11: production-kit recipe A/B previews over real fixture footage.

The fixture clip is REAL media generated in-test with the PINNED phase-0c
ffmpeg (T4 precedent: only ``testsrc2`` among test sources; encoders
h264_videotoolbox/aac): 3 s 320x240 @15 fps plus a 48 kHz sine tone, so
the existing preview renderer's probe/verify contract (CFR rate match,
48 kHz audio) holds exactly as it does for a real episode mezzanine.
Subtitle cue spans sit on exact-millisecond frames (15 fps -> multiples
of 3) because the renderer's SRT bounds are integer-ms by contract.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.foundation_io import sha256_file
from services.preview.models import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools
from services.production_kit.preview import (
    DEFAULT_DOMAINS,
    MANIFEST_NAME,
    SELECTION_STORE_RELATIVE,
    KitDomainSelectionV1,
    KitPreviewError,
    KitPreviewManifestV1,
    KitSelectionRecordV1,
    SnippetSpecV1,
    append_selection_entry,
    latest_selections,
    load_selection_record,
    plan_previews,
    recipe_selection_from_record,
)
from services.production_kit.recipe_select import select_recipe
from services.production_kit.registry import load_kit
from services.validate.selection_plan_store import initialize_plan_store

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.production_kit.models import ChannelProductionKitV1

FRAME_RATE = Fraction(15, 1)
TOTAL_FRAMES = 45  # 3 s at 15 fps
SUBTITLE_TEXT = "このテストは実素材の範囲から snippet を切り出して recipe を比較する"


@pytest.fixture(scope="module")
def kit() -> ChannelProductionKitV1:
    return load_kit()


@pytest.fixture(scope="module")
def tools() -> Iterator[PinnedTools]:
    try:
        yield load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="module")
def fixture_clip(
    tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Real mp4: lavfi testsrc2 + sine 48 kHz, h264_videotoolbox/aac."""

    media = tmp_path_factory.mktemp("kit-preview-fixture") / "clip.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return media


@pytest.fixture
def episode_root(tmp_path: Path, fixture_clip: Path) -> Path:
    """Fixture episode: committed selection store + episode footage."""

    root = tmp_path / "ep-kit-preview"
    root.mkdir()
    initialize_plan_store(
        root.joinpath(*SELECTION_STORE_RELATIVE).parent,
        episode_id="ep-kit-preview",
        base_version="v0",
    )
    media_dir = root / "run" / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "edit-source.mov").write_bytes(fixture_clip.read_bytes())
    return root


def _spec(clip: Path) -> SnippetSpecV1:
    return SnippetSpecV1(
        media_path=str(clip), start_frame=0, end_frame=TOTAL_FRAMES,
        subtitle_text=SUBTITLE_TEXT,
    )


# ---------------------------------------------------------------------------
# (a) subtitle domain: >=2 real mp4 candidates + validating manifest
# ---------------------------------------------------------------------------


def test_subtitle_domain_renders_two_candidates_from_real_footage(
    kit, episode_root: Path, fixture_clip: Path, tools: PinnedTools
) -> None:
    manifest = plan_previews(kit, episode_root, ("subtitle",), [_spec(fixture_clip)], tools=tools)

    assert [d.domain for d in manifest.domains] == ["subtitle"]
    domain = manifest.domains[0]
    # candidates follow the domain's semantic intents in stable sorted order
    assert domain.intents == ("keyword_text", "subtitle_track")
    assert [c.recipe_id for c in domain.candidates] == [
        "subtitle/emphasis",
        "subtitle/default",
    ]
    rendered = [
        episode_root / "kit-previews" / candidate.file
        for candidate in domain.candidates
    ]
    for path, candidate in zip(rendered, domain.candidates, strict=True):
        assert path.is_file(), path
        assert path.stat().st_size > 0
        assert candidate.sha256 == sha256_file(path)
    # snippet provenance is the episode footage itself
    assert domain.snippet.origin == "explicit"
    assert domain.snippet.media_path == str(fixture_clip)
    assert domain.snippet.duration_seconds == pytest.approx(3.0)
    # manifest validates strictly from disk (stale-state re-read)
    on_disk = KitPreviewManifestV1.model_validate_json(
        (episode_root / "kit-previews" / MANIFEST_NAME).read_bytes()
    )
    assert on_disk == manifest


def test_resolved_params_stay_within_recipe_bounds(
    kit, episode_root: Path, fixture_clip: Path, tools: PinnedTools
) -> None:
    manifest = plan_previews(
        kit, episode_root, DEFAULT_DOMAINS, [_spec(fixture_clip)], tools=tools
    )
    for domain in manifest.domains:
        for candidate in domain.candidates:
            recipe = next(r for r in kit.recipes if r.recipe_id == candidate.recipe_id)
            assert set(candidate.resolved_params) == set(recipe.parameter_bounds)
            for param, value in candidate.resolved_params.items():
                bound = recipe.parameter_bounds[param]
                assert float(bound.min) <= value <= float(bound.max)


def test_bounds_clipping_applies_through_taste_evidence(
    kit, episode_root: Path, fixture_clip: Path, tools: PinnedTools
) -> None:
    """Out-of-bounds taste evidence is clipped by the task-37 path (999px -> 48px)."""

    manifest = plan_previews(
        kit,
        episode_root,
        ("subtitle",),
        [_spec(fixture_clip)],
        tools=tools,
        taste_evidence=[{"evidence_id": "taste-test", "param": "font_size", "value": 999.0}],
    )
    default = next(
        c for c in manifest.domains[0].candidates if c.recipe_id == "subtitle/default"
    )
    assert default.resolved_params["font_size"] == 48.0
    assert "clipped font_size 999.0" in default.selection_rationale


# ---------------------------------------------------------------------------
# (b) audio + color domains render through the same real path
# ---------------------------------------------------------------------------


def test_audio_and_color_domains_render(kit, episode_root: Path, fixture_clip: Path,
                                        tools: PinnedTools) -> None:
    manifest = plan_previews(
        kit, episode_root, ("audio", "color"), [_spec(fixture_clip)], tools=tools
    )
    assert [d.domain for d in manifest.domains] == ["audio", "color"]
    for domain in manifest.domains:
        assert len(domain.candidates) >= 1
        for candidate in domain.candidates:
            path = episode_root / "kit-previews" / candidate.file
            assert path.is_file(), path


# ---------------------------------------------------------------------------
# (c) failure paths: typed refusals
# ---------------------------------------------------------------------------


def test_missing_committed_selection_is_a_typed_refusal(
    kit, tmp_path: Path, fixture_clip: Path
) -> None:
    bare = tmp_path / "ep-no-selection"
    bare.mkdir()

    with pytest.raises(KitPreviewError) as raised:
        plan_previews(kit, bare, ("subtitle",), [_spec(fixture_clip)])

    assert raised.value.code == "selection-missing"
    assert "PLAN_COMMITTED" in raised.value.detail


def test_unknown_domain_is_typed_refusal(kit, episode_root: Path, fixture_clip: Path) -> None:
    with pytest.raises(KitPreviewError) as raised:
        plan_previews(kit, episode_root, ("vfx",), [_spec(fixture_clip)])
    assert raised.value.code == "unknown-domain"


def test_spec_span_beyond_media_is_typed_refusal(
    kit, episode_root: Path, fixture_clip: Path, tools: PinnedTools
) -> None:
    bogus = SnippetSpecV1(
        media_path=str(fixture_clip), start_frame=0, end_frame=TOTAL_FRAMES + 500
    )
    with pytest.raises(KitPreviewError) as raised:
        plan_previews(kit, episode_root, ("subtitle",), [bogus], tools=tools)
    assert raised.value.code == "snippet-source-missing"


# ---------------------------------------------------------------------------
# (d) selection record round-trips through RecipeSelection
# ---------------------------------------------------------------------------


def test_recorded_selection_round_trips_to_recipe_selection(
    kit, tmp_path: Path
) -> None:
    record_path = tmp_path / "kit-selections.json"
    record = append_selection_entry(
        record_path,
        "ep-kit-preview",
        KitDomainSelectionV1(
            domain="subtitle",
            recipe_id="subtitle/emphasis",
            semantic_intent="keyword_text",
            note="強調ありで",
            recorded_at="2026-08-23T00:00:00Z",
        ),
    )
    record = append_selection_entry(
        record_path,
        "ep-kit-preview",
        KitDomainSelectionV1(
            domain="audio",
            recipe_id=None,
            semantic_intent=None,
            note="現状維持",
            recorded_at="2026-08-23T00:01:00Z",
        ),
    )

    # strict re-read round-trips (stale-state revalidation)
    assert load_selection_record(record_path) == record
    # a recipe choice reproduces the SAME RecipeSelection select_recipe resolves
    resolved = recipe_selection_from_record(kit, record, "subtitle")
    assert resolved is not None
    assert resolved == select_recipe(kit, "keyword_text")
    # a none-choice (どちらも不要/現状維持) resolves to no recipe
    assert recipe_selection_from_record(kit, record, "audio") is None
    # latest entry per domain wins
    assert latest_selections(record)["audio"].note == "現状維持"


def test_unknown_recorded_recipe_is_a_typed_refusal(kit) -> None:
    record = KitSelectionRecordV1(
        episode_id="ep-kit-preview",
        entries=(
            KitDomainSelectionV1(
                domain="color",
                recipe_id="color/does-not-exist",
                semantic_intent="color_look",
                recorded_at="2026-08-23T00:00:00Z",
            ),
        ),
    )
    with pytest.raises(KitPreviewError) as raised:
        recipe_selection_from_record(kit, record, "color")
    assert raised.value.code == "unknown-recipe"


def test_corrupt_selection_record_fails_strict_revalidation(tmp_path: Path) -> None:
    path = tmp_path / "kit-selections.json"
    path.write_bytes(b'{"schema_version": "kit-selection-v1", "entries": [')
    with pytest.raises(KitPreviewError) as raised:
        load_selection_record(path)
    assert raised.value.code == "record-invalid"
