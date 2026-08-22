from __future__ import annotations

import pytest

from services.media_intelligence.models import (
    AudioMeasurements,
    MediaIntelligenceArtifact,
    SilenceSegment,
)
from services.media_intelligence.reconcile import (
    ConformContext,
    LocalEvidenceBatch,
    LocalSourceEvidence,
    UnknownSourceReconcileError,
    reconcile,
)
from services.media_intelligence.shot_identity import derive_shot_id


def _shot_dict(
    shot_id: str,
    start: int,
    end: int,
    *,
    best_frame: int | None = None,
) -> dict:
    bf = best_frame if best_frame is not None else start + 1
    return {
        "shot_id": shot_id,
        "source_span": {"start_frame": start, "end_frame": end},
        "description": "test shot",
        "visual": {"shot_size": "medium", "camera_motion": "static", "quality_flags": []},
        "editorial": {
            "role": "coverage",
            "select_potential": "medium",
            "best_moment": {"frame": bf, "why": "moment"},
            "pacing": "moderate",
            "cuttability": {"in": "clean", "out": "clean"},
        },
        "transcript_refs": [],
        "evidence_refs": [],
        "confidence": {"editorial": "medium", "visual": "medium"},
    }


def _make_mcp(
    episode_id: str = "ep-001",
    source_id: str = "src-001",
    shots: list[dict] | None = None,
) -> MediaIntelligenceArtifact:
    if shots is None:
        shots = [_shot_dict("shot-001", 0, 100), _shot_dict("shot-002", 100, 200)]
    return MediaIntelligenceArtifact.model_validate(
        {
            "schema_version": "media-intelligence-v2",
            "episode_id": episode_id,
            "sources": [{"source_id": source_id}],
            "shots": shots,
        }
    )


def test_determinism_when_same_inputs_then_byte_identical() -> None:
    conform = ConformContext(source_ids=("src-001",))
    mcp = _make_mcp()
    local = LocalEvidenceBatch(
        sources=(
            LocalSourceEvidence(
                source_id="src-001",
                scene_boundaries=(50,),
                silence_segments=(
                    SilenceSegment(start_frame=10, end_frame=20),
                ),
            ),
        )
    )

    first = reconcile(local, mcp, conform, boundary_tolerance_frames=2)
    second = reconcile(local, mcp, conform, boundary_tolerance_frames=2)

    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert first.model_dump(mode="json", by_alias=True) == second.model_dump(
        mode="json", by_alias=True
    )

    expected_id = derive_shot_id("src-001", 0, 100, "mcp")
    assert first.shots[0].shot_id == expected_id
    assert second.shots[0].shot_id == expected_id
    assert first.shots[1].shot_id == derive_shot_id("src-001", 100, 200, "mcp")


def test_unknown_source_id_is_typed_rejection() -> None:
    conform = ConformContext(source_ids=("src-allowed",))
    mcp_unknown = _make_mcp(source_id="src-unknown")

    local = LocalEvidenceBatch(sources=())
    with pytest.raises(UnknownSourceReconcileError):
        reconcile(local, mcp_unknown, conform)

    # local unknown also rejected
    mcp_ok = _make_mcp(source_id="src-allowed")
    local_unknown = LocalEvidenceBatch(
        sources=(LocalSourceEvidence(source_id="src-unknown", scene_boundaries=()),)
    )
    with pytest.raises(UnknownSourceReconcileError):
        reconcile(local_unknown, mcp_ok, conform)


def test_boundary_disagreement_beyond_tolerance_then_conflict_record_and_both_retained() -> None:
    conform = ConformContext(source_ids=("src-001",))
    mcp = _make_mcp(
        shots=[_shot_dict("shot-001", 0, 100), _shot_dict("shot-002", 100, 200)]
    )
    # local boundary at 110 disagrees with MCP boundary at 100 by 10 (>2)
    local = LocalEvidenceBatch(
        sources=(
            LocalSourceEvidence(
                source_id="src-001",
                scene_boundaries=(110,),
                silence_segments=(SilenceSegment(start_frame=5, end_frame=15),),
            ),
        )
    )

    merged = reconcile(local, mcp, conform, boundary_tolerance_frames=2)

    # MCP evidence retained: spans unchanged and deterministic IDs
    assert merged.shots[0].source_span.start_frame == 0
    assert merged.shots[0].source_span.end_frame == 100
    assert merged.shots[1].source_span.start_frame == 100

    # conflict record present on the shot whose MCP boundary conflicted
    conflict_shot = next(s for s in merged.shots if s.source_span.start_frame == 100)
    assert conflict_shot.provenance is not None
    assert any(p.field == "boundary_conflict" for p in conflict_shot.provenance)
    # local boundary explicitly retained
    assert any("local-boundary:110" in r for r in conflict_shot.evidence_refs)
    # confidence string carries both local and mcp frame numbers
    conflict_rec = next(p for p in conflict_shot.provenance if p.field == "boundary_conflict")
    assert "local=110" in conflict_rec.confidence  # type: ignore[union-attr]
    assert "mcp=100" in conflict_rec.confidence  # type: ignore[union-attr]

    # both evidences retained: MCP span + local boundary provenance
    assert any(
        p.field == "boundary_conflict_local_boundary" and p.confidence == "110"
        for p in conflict_shot.provenance
    )


def test_agreement_within_tolerance_then_clean_merge_no_conflict() -> None:
    conform = ConformContext(source_ids=("src-001",))
    mcp = _make_mcp(
        shots=[_shot_dict("shot-001", 0, 100), _shot_dict("shot-002", 100, 200)]
    )
    # local boundary 101 is within tolerance 2 of MCP boundary 100
    local = LocalEvidenceBatch(
        sources=(
            LocalSourceEvidence(
                source_id="src-001",
                scene_boundaries=(101,),
                silence_segments=(SilenceSegment(start_frame=10, end_frame=20),),
                audio_measurements=AudioMeasurements(loudness_db=-14.0),
            ),
        )
    )

    merged = reconcile(local, mcp, conform, boundary_tolerance_frames=2)

    # no conflict record anywhere
    for shot in merged.shots:
        if shot.provenance is not None:
            assert not any(p.field == "boundary_conflict" for p in shot.provenance)

    # non-conflicting local measurements attached as optional evidence
    # silence overlapping first shot should be present
    first = next(s for s in merged.shots if s.source_span.start_frame == 0)
    assert first.silence_segments is not None
    assert len(first.silence_segments) == 1
    assert first.silence_segments[0].start_frame == 10
    # audio measurements attached
    assert first.audio_measurements is not None
    assert first.audio_measurements.loudness_db == -14.0
