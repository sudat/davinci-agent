from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest_phase1 import (
    PHASE_1_FIXTURE_IDS,
    Phase1TechnicalFixtureManifest,
)
from services.ingest.eligibility import (
    EligibilityDeclared,
    EpisodeEligibilityBundle,
    classify_episode,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-1-technical")
FORBIDDEN_IMPORT_ROOTS = {"whisper", "semgrep", "numpy", "duckdb", "subprocess", "ffmpeg"}
FORBIDDEN_NAME_PARTS = ("detect_privacy", "scan_content", "transcribe", "ocr")


def bundle(**overrides: object) -> EpisodeEligibilityBundle:
    declared: dict[str, object] = {
        "language": "ja",
        "principal_video_count": 1,
        "audio_present": True,
        "vfr": False,
        "cfr_normalizable": True,
        "total_duration_sec": 1200,
        "speaker_count": 1,
    }
    declared.update(overrides)
    return EpisodeEligibilityBundle(
        episode_id="ep-test",
        declared=EligibilityDeclared.model_validate(declared),
    )


def test_all_five_frozen_fixture_episodes_classify_supported() -> None:
    for fixture_id in PHASE_1_FIXTURE_IDS:
        manifest = Phase1TechnicalFixtureManifest.model_validate_json(
            (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        )
        result = classify_episode(
            EpisodeEligibilityBundle(
                episode_id=manifest.fixture_id,
                declared=EligibilityDeclared.model_validate(
                    json.loads(manifest.eligibility.declared.model_dump_json())
                ),
            )
        )
        assert result.status == manifest.eligibility.expected_status
        assert result.reasons == ()
        assert result.human_gates == ()


def test_missing_audio_is_unsupported() -> None:
    result = classify_episode(bundle(audio_present=False))
    assert result.status == "unsupported"
    assert [reason.code for reason in result.reasons] == ["audio-absent"]


def test_missing_principal_video_is_unsupported() -> None:
    result = classify_episode(bundle(principal_video_count=0))
    assert result.status == "unsupported"
    assert "video-absent" in [reason.code for reason in result.reasons]


def test_unnormalizable_vfr_is_unsupported() -> None:
    result = classify_episode(bundle(vfr=True, cfr_normalizable=False))
    assert result.status == "unsupported"
    assert "vfr-unnormalizable" in [reason.code for reason in result.reasons]


def test_normalizable_vfr_stays_supported() -> None:
    assert classify_episode(bundle(vfr=True, cfr_normalizable=True)).status == "supported"


def test_oversized_source_is_assisted() -> None:
    result = classify_episode(bundle(total_duration_sec=5401))
    assert result.status == "assisted"
    assert [reason.code for reason in result.reasons] == ["source-duration-over-budget"]


def test_multi_principal_video_is_assisted() -> None:
    result = classify_episode(bundle(principal_video_count=2))
    assert result.status == "assisted"
    assert [reason.code for reason in result.reasons] == ["multi-principal-video"]


def test_non_japanese_language_is_assisted() -> None:
    result = classify_episode(bundle(language="en"))
    assert result.status == "assisted"
    assert "language-out-of-contract" in [reason.code for reason in result.reasons]


def test_declared_privacy_flags_pass_through_as_human_gates_without_rerouting() -> None:
    result = classify_episode(bundle(privacy_flags=("face-at-00m12s",)))
    assert result.status == "supported"
    assert result.human_gates[0].kind == "privacy"
    assert result.human_gates[0].flag == "face-at-00m12s"


def test_declared_rights_flags_pass_through_as_human_gates() -> None:
    result = classify_episode(bundle(rights_flags=("bgm-license-c",)))
    assert result.status == "supported"
    assert result.human_gates[0].kind == "rights"


def test_classification_depends_only_on_declared_fields() -> None:
    first = classify_episode(bundle())
    second = classify_episode(
        EpisodeEligibilityBundle(episode_id="ep-other", declared=bundle().declared)
    )
    assert first.status == second.status
    assert first.reasons == second.reasons


def test_malformed_declared_bundle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EligibilityDeclared.model_validate({"language": "ja"})  # type: ignore[call-overload]


def test_no_automated_privacy_detection() -> None:
    source = Path("services/ingest/eligibility.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".", maxsplit=1)[0])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert roots & FORBIDDEN_IMPORT_ROOTS == set()
    assert not any(part in name for name in names for part in FORBIDDEN_NAME_PARTS)
    assert "classify_episode" in names
