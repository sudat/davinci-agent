from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.media_intelligence.models import (
    PRD_7_2_FIELD_MAPPING,
    MediaIntelligenceArtifact,
)

# ---------------------------------------------------------------------------
# PRD 7.3 illustrative JSON (docs/prd/PRD_v4.3.md:488-515, copied verbatim)
# ---------------------------------------------------------------------------

PRD_7_3_EXAMPLE = {
    "schema_version": "media-intelligence-v2",
    "episode_id": "ep-001",
    "sources": [],
    "shots": [
        {
            "shot_id": "shot-001",
            "source_span": {"start_frame": 1200, "end_frame": 1840},
            "description": "出演者が製品を持ち上げて比較点を説明する",
            "visual": {
                "shot_size": "medium_close",
                "camera_motion": "handheld",
                "quality_flags": [],
            },
            "editorial": {
                "role": "coverage",
                "select_potential": "high",
                "best_moment": {"frame": 1510, "why": "比較差が視覚的に分かる"},
                "pacing": "moderate",
                "cuttability": {"in": "clean", "out": "clean"},
            },
            "transcript_refs": ["tr-018"],
            "evidence_refs": ["mcp-analysis:abc"],
            "confidence": {"editorial": "medium", "visual": "high"},
        }
    ],
}


def test_prd_7_3_round_trip_when_example_is_valid() -> None:
    model = MediaIntelligenceArtifact.model_validate(PRD_7_3_EXAMPLE)

    assert model.schema_version == "media-intelligence-v2"
    assert model.episode_id == "ep-001"
    assert len(model.shots) == 1

    dumped = model.model_dump(mode="json", by_alias=True)
    reparsed = MediaIntelligenceArtifact.model_validate(dumped)

    assert reparsed == model
    # semantic content preserved for the core fields
    assert dumped["shots"][0]["source_span"]["start_frame"] == 1200
    assert dumped["shots"][0]["source_span"]["end_frame"] == 1840
    assert dumped["shots"][0]["editorial"]["cuttability"]["in"] == "clean"
    assert dumped["shots"][0]["editorial"]["cuttability"]["out"] == "clean"

    json_bytes = model.model_dump_json(by_alias=True)
    reparsed_json = MediaIntelligenceArtifact.model_validate_json(json_bytes)
    assert reparsed_json == model


def test_float_frame_is_rejected_when_start_frame_is_float() -> None:
    mutated = {
        "schema_version": "media-intelligence-v2",
        "episode_id": "ep-001",
        "sources": [],
        "shots": [
            {
                "shot_id": "shot-001",
                "source_span": {"start_frame": 1.5, "end_frame": 10},
                "description": "float must be rejected",
                "visual": {
                    "shot_size": "wide",
                    "camera_motion": "static",
                    "quality_flags": [],
                },
                "editorial": {
                    "role": "coverage",
                    "select_potential": "low",
                    "best_moment": {"frame": 2, "why": "why"},
                    "pacing": "moderate",
                    "cuttability": {"in": "clean", "out": "clean"},
                },
                "transcript_refs": [],
                "evidence_refs": [],
                "confidence": {"editorial": "low", "visual": "low"},
            }
        ],
    }

    with pytest.raises(ValidationError):
        MediaIntelligenceArtifact.model_validate(mutated)

    with pytest.raises(ValidationError):
        MediaIntelligenceArtifact.model_validate_json(json.dumps(mutated))


PRD_7_2_CATEGORIES = [
    "transcript and word/segment timing",
    "speaker identity when available",
    "silence and filler evidence",
    "loudness/energy/ambient measurements",
    "shot boundaries",
    "shot size and framing",
    "camera movement",
    "primary subject and action",
    "location and visible text",
    "visual quality problems",
    "editorial role",
    "select potential",
    "best moment candidate",
    "pacing and stillness type",
    "cut-in/cut-out quality",
    "visual similarity/embedding references",
    "objects or product references",
    "face/reaction cues when confidently available",
    "provenance and confidence per field",
]


def test_every_prd_7_2_category_appears_in_field_mapping() -> None:
    for category in PRD_7_2_CATEGORIES:
        assert category in PRD_7_2_FIELD_MAPPING, f"missing category: {category}"
        assert PRD_7_2_FIELD_MAPPING[category]


def test_schema_export_exists_and_validates() -> None:
    export_path = Path("schemas/contracts/media-intelligence-v2.json")
    assert export_path.is_file(), f"missing export: {export_path}"

    file_bytes = export_path.read_bytes()
    file_schema = json.loads(file_bytes)

    expected_schema = MediaIntelligenceArtifact.model_json_schema()
    assert file_schema == expected_schema

    canonical = json.dumps(
        file_schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode() + b"\n"
    assert file_bytes == canonical
