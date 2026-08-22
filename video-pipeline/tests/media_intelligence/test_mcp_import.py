from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate
from services.mcp_client.response_normalize import (
    NormalizationError,
    normalize_deep_shot_analysis,
    normalize_media_analysis_standard,
)
from services.media_intelligence.mcp_import import (
    AnalysisLineage,
    MissingLineageError,
    import_deep_shots,
    import_standard_analysis,
)
from services.media_intelligence.models import MediaIntelligenceArtifact
from services.media_intelligence.shot_identity import derive_shot_id

FIXTURES_DIR = Path(__file__).parents[1] / "mcp_client" / "fixtures"
EDIT_RATE = RationalFrameRate(num=30, den=1)
EPISODE_ID = "ep-001"
SOURCE_ID = "src-001"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_bytes())


def _standard_payload() -> object:
    return _load_json("media-analysis-standard.json")


def _deep_payload() -> object:
    return json.loads((FIXTURES_DIR / "deep-shot-analysis.json").read_bytes())


def _lineage(**overrides: object) -> AnalysisLineage:
    base: dict[str, object] = {
        "provider": "host_chat_paths",
        "provider_version": "1.29.0",
        "tool": "media_analysis-standard",
    }
    base.update(overrides)
    return AnalysisLineage.model_validate(base)


def _deep_lineage(**overrides: object) -> AnalysisLineage:
    base: dict[str, object] = {
        "provider": "host_chat_paths",
        "provider_version": "1.29.0",
        "tool": "media_analysis-deep",
    }
    base.update(overrides)
    return AnalysisLineage.model_validate(base)


# ---------------------------------------------------------------------------
# (a) deterministic ids — double-run identical
# ---------------------------------------------------------------------------


def test_standard_shots_have_deterministic_ids_double_run_identical() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage()

    first = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )
    second = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert [s.shot_id for s in first.shots] == [s.shot_id for s in second.shots]
    assert len(first.shots) == 2

    for shot in first.shots:
        expected = derive_shot_id(
            SOURCE_ID,
            int(shot.source_span.start_frame),
            int(shot.source_span.end_frame),
            lineage.provider,
            lineage.provider_version,
        )
        assert shot.shot_id == expected


def test_deep_shots_have_deterministic_ids_double_run_identical() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())
    lineage = _deep_lineage()

    first = import_deep_shots(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )
    second = import_deep_shots(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert [s.shot_id for s in first.shots] == [s.shot_id for s in second.shots]
    assert len(first.shots) == 2


# ---------------------------------------------------------------------------
# (b) lineage present on every shot
# ---------------------------------------------------------------------------


def test_standard_lineage_present_on_every_shot() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage(provider_version="1.29.0")

    artifact = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    assert len(artifact.shots) == 2
    for shot in artifact.shots:
        assert shot.provenance is not None
        assert len(shot.provenance) >= 1
        # at least one record carries the provider lineage
        assert any(
            p.provider == lineage.provider and p.provider_version == lineage.provider_version
            for p in shot.provenance
        )
        # tool is encoded in provenance confidence
        assert any(
            p.confidence is not None and lineage.tool in p.confidence
            for p in shot.provenance
        )


def test_deep_lineage_present_on_every_shot() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())
    lineage = _deep_lineage(provider_version="9.9.9")

    artifact = import_deep_shots(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    for shot in artifact.shots:
        assert shot.provenance is not None
        assert any(
            p.provider == lineage.provider and p.provider_version == "9.9.9"
            for p in shot.provenance
        )


# ---------------------------------------------------------------------------
# (c) lineage stripped → MissingLineageError
# ---------------------------------------------------------------------------


def test_standard_missing_lineage_raises_missing_lineage_error() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())

    with pytest.raises(MissingLineageError):
        import_standard_analysis(
            typed,
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            edit_rate=EDIT_RATE,
            provenance=None,  # type: ignore[arg-type]
        )


def test_deep_missing_lineage_raises_missing_lineage_error() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())

    with pytest.raises(MissingLineageError):
        import_deep_shots(
            typed,
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            edit_rate=EDIT_RATE,
            provenance=None,  # type: ignore[arg-type]
        )


def test_standard_lineage_missing_required_field_raises_missing_lineage_error() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())

    # dict missing provider_version — _require_lineage must raise MissingLineageError
    with pytest.raises(MissingLineageError) as exc:
        import_standard_analysis(
            typed,
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            edit_rate=EDIT_RATE,
            provenance={"provider": "host_chat_paths", "tool": "media_analysis-standard"},  # type: ignore[arg-type]
        )
    assert "provider_version" in str(exc.value).lower()


def test_deep_lineage_missing_provider_raises_missing_lineage_error() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())

    with pytest.raises(MissingLineageError):
        import_deep_shots(
            typed,
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            edit_rate=EDIT_RATE,
            provenance={"provider_version": "1.0", "tool": "media_analysis-deep"},  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# (d) evidence_refs present + provider-path style
# ---------------------------------------------------------------------------


def test_standard_evidence_refs_present_and_provider_path_style() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage()

    artifact = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    for shot in artifact.shots:
        assert len(shot.evidence_refs) >= 1
        assert any(ref.startswith("mcp-analysis:") for ref in shot.evidence_refs)
        # must contain the tool name
        assert any(lineage.tool in ref for ref in shot.evidence_refs)
        # must not be a raw DB ref — evidence only, provider path goes in provenance
        assert not any(ref.startswith(("db:", "sqlite:")) for ref in shot.evidence_refs)


def test_deep_evidence_refs_present_and_provider_path_style() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())
    lineage = _deep_lineage()

    artifact = import_deep_shots(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    for shot in artifact.shots:
        assert len(shot.evidence_refs) >= 1
        assert any(ref.startswith("mcp-analysis:") for ref in shot.evidence_refs)


def test_evidence_ref_includes_params_digest_when_present() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage(params_digest="abc123")

    artifact = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    for shot in artifact.shots:
        assert any("abc123" in ref for ref in shot.evidence_refs)


# ---------------------------------------------------------------------------
# (e) round-trip through the task-12 canonical model
# ---------------------------------------------------------------------------


def test_standard_round_trip_through_canonical_model() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage()

    artifact = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    dumped = artifact.model_dump(mode="json", by_alias=True)
    reparsed = MediaIntelligenceArtifact.model_validate(dumped)
    assert reparsed == artifact

    json_bytes = artifact.model_dump_json(by_alias=True)
    reparsed_json = MediaIntelligenceArtifact.model_validate_json(json_bytes)
    assert reparsed_json == artifact


def test_deep_round_trip_through_canonical_model() -> None:
    typed = normalize_deep_shot_analysis(_deep_payload())
    lineage = _deep_lineage()

    artifact = import_deep_shots(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    dumped = artifact.model_dump(mode="json", by_alias=True)
    reparsed = MediaIntelligenceArtifact.model_validate(dumped)
    assert reparsed == artifact

    json_bytes = artifact.model_dump_json(by_alias=True)
    assert MediaIntelligenceArtifact.model_validate_json(json_bytes) == artifact


# ---------------------------------------------------------------------------
# Extra: typed import error on unknown field (mirrors task-9 discipline)
# ---------------------------------------------------------------------------


def test_unknown_field_in_raw_payload_raises_typed_import_error() -> None:
    raw: dict[str, object] = dict(_standard_payload())
    raw["unknown_field"] = "oops"

    with pytest.raises((NormalizationError, ValidationError, Exception)) as exc:
        import_standard_analysis(  # type: ignore[arg-type]
            raw,
            episode_id=EPISODE_ID,
            source_id=SOURCE_ID,
            edit_rate=EDIT_RATE,
            provenance=_lineage(),
        )

    # must name the unknown key or signal extra_forbidden
    msg = str(exc.value).lower()
    assert "unknown" in msg or "extra" in msg or "unknown_field" in msg


def test_source_span_frames_are_strict_int() -> None:
    typed = normalize_media_analysis_standard(_standard_payload())
    lineage = _lineage()

    artifact = import_standard_analysis(
        typed,
        episode_id=EPISODE_ID,
        source_id=SOURCE_ID,
        edit_rate=EDIT_RATE,
        provenance=lineage,
    )

    for shot in artifact.shots:
        assert isinstance(shot.source_span.start_frame, int)
        assert isinstance(shot.source_span.end_frame, int)
        assert shot.source_span.end_frame >= shot.source_span.start_frame


def test_recorded_standard_fixture_is_rejected_by_strict_normalizer() -> None:
    # Task-11 decision: recorded payloads use a different V2 vendor class
    # than the strict seed contract — rejection is by-design and locked.
    recorded = _load_json("media-analysis-standard.recorded.json")

    with pytest.raises(NormalizationError):
        normalize_media_analysis_standard(recorded)


def test_recorded_deep_fixture_is_rejected_by_strict_normalizer() -> None:
    recorded = _load_json("deep-shot-analysis.recorded.json")

    with pytest.raises(NormalizationError):
        normalize_deep_shot_analysis(recorded)
