from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.gates import PHASE_0B_VARIANTS
from services.toolchain.normalization import NormalizationSection

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0b")
GOLDEN_EXPECTED = Path("tests/goldens/reference/phase-0b/expected.json")
LOCK = Path("config/toolchains/phase-0b-v1.json")


def _manifests() -> dict[str, Phase0BFixtureManifest]:
    parsed = {}
    for fixture_id in PHASE_0B_VARIANTS:
        raw = (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        parsed[fixture_id] = Phase0BFixtureManifest.model_validate_json(raw)
    return parsed


def _golden() -> dict[str, object]:
    payload = json.loads(GOLDEN_EXPECTED.read_bytes())
    variants = payload.get("variants")
    assert isinstance(variants, dict)
    return variants


def test_all_manifests_are_canonical_and_valid() -> None:
    for fixture_id in PHASE_0B_VARIANTS:
        raw = (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        manifest = Phase0BFixtureManifest.model_validate_json(raw)
        assert manifest.fixture_id == fixture_id
        assert raw == manifest.canonical_bytes()


def test_manifest_conversion_tables_match_independent_golden() -> None:
    golden = _golden()
    for fixture_id, manifest in _manifests().items():
        table = golden.get(fixture_id)
        assert isinstance(table, dict)
        for target in ("cfr30", "cfr24"):
            expected = table.get(target)
            assert isinstance(expected, dict)
            conversion = manifest.conversions[target]
            assert expected["output_frames"] == conversion.output_frames
            assert expected["dropped_source_frames"] == list(conversion.dropped_source_frames)
            assert expected["duplicated_source_frames"] == list(
                conversion.duplicated_source_frames
            )


def test_manifest_anchor_tables_match_independent_golden() -> None:
    golden = _golden()
    for fixture_id, manifest in _manifests().items():
        table = golden.get(fixture_id)
        assert isinstance(table, dict)
        anchors = {
            anchor["pulse_id"]: anchor
            for anchor in table["audio_anchor_ticks"]
            if isinstance(anchor, dict)
        }
        for anchor in manifest.audio_sample_anchors:
            expected = anchors[anchor.pulse_id]
            assert anchor.source_sample == expected["source_sample"]
            assert anchor.cfr30_tick == expected["cfr30_tick"]
            assert anchor.cfr24_tick == expected["cfr24_tick"]
        cues = table["subtitle_anchor_ticks"]
        assert isinstance(cues, dict)
        for cue in manifest.subtitle_anchors:
            expected = cues[cue.cue_id]
            assert isinstance(expected, dict)
            assert [cue.cfr30_tick_span.start_frame, cue.cfr30_tick_span.end_frame] == expected[
                "cfr30"
            ]
            assert [cue.cfr24_tick_span.start_frame, cue.cfr24_tick_span.end_frame] == expected[
                "cfr24"
            ]


def test_canonical_conversion_accounting_examples() -> None:
    manifests = _manifests()
    cfr24 = manifests["p0b-cfr24"].conversions["cfr30"]
    assert cfr24.output_frames == 750
    assert len(cfr24.duplicated_source_frames) == 150
    assert cfr24.dropped_source_frames == ()
    ntsc2997 = manifests["p0b-ntsc2997"].conversions["cfr30"]
    assert ntsc2997.output_frames == 601
    assert len(ntsc2997.duplicated_source_frames) == 1
    ntsc5994 = manifests["p0b-ntsc5994"].conversions["cfr30"]
    assert ntsc5994.output_frames == 300
    assert len(ntsc5994.dropped_source_frames) == 300
    vfr = manifests["p0b-vfr-2-3-cadence"].conversions["cfr30"]
    assert vfr.output_frames == 150
    assert len(vfr.duplicated_source_frames) == 30
    assert manifests["p0b-vfr-2-3-cadence"].conversions["cfr24"].output_frames == 120


def test_audio_offset_fixture_shifts_anchors_by_1024_samples() -> None:
    manifest = _manifests()["p0b-audio-offset1024"]
    assert manifest.generation.audio.content_offset_samples == 1024
    assert manifest.audio_sample_anchors[0].source_sample == 1024 + 240000


def test_rotate90_fixture_pins_display_dimensions() -> None:
    manifest = _manifests()["p0b-rotate90"]
    assert manifest.rotation is not None
    assert (manifest.rotation.display_width, manifest.rotation.display_height) == (1080, 1920)
    assert manifest.generation.post[0].rotation_degrees == 90


def test_observed_result_field_is_rejected() -> None:
    raw = json.loads((MANIFEST_DIR / "p0b-cfr24.json").read_bytes())
    raw["observed_source_probe"] = {"nb_frames": "600"}
    with pytest.raises(ValidationError, match="observed"):
        Phase0BFixtureManifest.model_validate(raw)


def test_observed_expectation_basis_is_rejected() -> None:
    raw = json.loads((MANIFEST_DIR / "p0b-cfr24.json").read_bytes())
    raw["expectation_basis"] = "observed-from-production"
    with pytest.raises(ValidationError):
        Phase0BFixtureManifest.model_validate(raw)


def test_recipe_argv_reference_only_frozen_filters_and_encoders() -> None:
    section = NormalizationSection.model_validate_json(
        Path("config/toolchains/pins/normalize-recipes.json").read_bytes()
    )
    assert tuple(recipe.fixture_id for recipe in section.recipes) == PHASE_0B_VARIANTS
    for recipe in section.recipes:
        joined = "\n".join(recipe.argv)
        assert "{ffmpeg}" in joined
        assert "{input}" in joined
        assert "{output}" in joined


def _pin_payload() -> dict[str, object]:
    payload: dict[str, object] = json.loads(
        Path("config/toolchains/pins/normalize-recipes.json").read_bytes()
    )
    return payload


def test_recipe_with_unfrozen_filter_is_rejected() -> None:
    payload = _pin_payload()
    recipes = payload["recipes"]
    assert isinstance(recipes, list)
    first = recipes[0]
    assert isinstance(first, dict)
    argv = first["argv"]
    assert isinstance(argv, list)
    argv[argv.index("-vf") + 1] = "transpose=1,fps=30"
    with pytest.raises(ValidationError, match="frozen"):
        NormalizationSection.model_validate_json(json.dumps(payload))


def test_recipe_missing_variant_is_rejected() -> None:
    payload = _pin_payload()
    recipes = payload["recipes"]
    assert isinstance(recipes, list)
    payload["recipes"] = recipes[:5]
    with pytest.raises(ValidationError, match="six"):
        NormalizationSection.model_validate_json(json.dumps(payload))
