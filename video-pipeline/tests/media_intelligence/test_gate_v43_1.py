"""Gate V43-1 — Progressive Media Intelligence (PRD 1573-1585, plan task 19).

Three materially different synthetic fixture classes (speaker+B-roll /
visual-first / screen-product) are driven through the full W3 chain:
reconcile (T14) → v2 index (T15) → progressive plan+budget (T16) → moment
deep review (T17) → recall audit (T18).  Gate exit criteria asserted:

(a) a valuable visually driven moment is retrievable with NO speech;
(b) B-roll candidates are retrievable by meaning (ja AND en queries);
(c) the MCP internal DB is NOT the authoritative artifact — canonical
    fixture bytes exist and every index row's lineage points at them;
(d) the recall-audit sentinel runs on every fixture (report generated);
(e) an AnalysisBudgetV1 exists per fixture with trigger reasons.

Plus: benchmark counters are deterministic (injected monotonic → byte
identical double run), reanalysis after one-shot correction recomputes
dependent work only, and the committed gate summary / backend transition
record match freshly computed evidence (parse artifacts, not logs).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from services.media_intelligence.benchmark import (
    BenchmarkPolicy,
    aggregate_runs,
    deterministic_summary,
    run_benchmark,
    run_chain,
)
from services.media_intelligence.budget import TRIGGER_REASONS
from services.media_intelligence.models import MediaIntelligenceArtifact
from services.media_intelligence.progressive import ProgressivePolicy
from services.media_intelligence.recall_audit import RecallAuditPolicy
from services.media_query.index_v2 import artifact_content_sha
from services.media_query.query_v2 import MediaQueryApiV2
from services.media_query.v2_models import (
    BestMomentsRequest,
    FrameSpan,
    MomentReviewsRequest,
    SemanticSearchRequest,
    ShotDetailRequest,
    ShotsRequest,
    TranscriptRangeRequest,
    V2Pagination,
)
from tests.media_intelligence.gate_fixture_gen import canonical_fixture_bytes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "gate-v43-1"
FIXTURE_IDS = ("speaker-broll", "visual-first", "screen-product")
CAPABILITIES_ROOT = Path(__file__).parents[2] / "capabilities" / "v4.3" / "runs"
GATE_POLICY = BenchmarkPolicy(
    progressive=ProgressivePolicy(recall_audit_sample_count=2),
    recall_audit=RecallAuditPolicy(),
)


def load_fixture(fixture_id: str) -> MediaIntelligenceArtifact:
    path = FIXTURE_ROOT / fixture_id / "artifact.json"
    return MediaIntelligenceArtifact.model_validate_json(path.read_bytes())


def _fake_monotonic() -> Callable[[], float]:
    ticks = iter(range(0, 100_000, 7))

    def monotonic() -> float:
        return float(next(ticks))

    return monotonic


def test_fixture_bytes_are_canonical_and_double_build_identical() -> None:
    """Given the committed fixtures, When regenerated deterministically,
    Then the bytes are byte-identical (stale fixtures fail)."""
    for fixture_id in FIXTURE_IDS:
        path = FIXTURE_ROOT / fixture_id / "artifact.json"
        assert path.is_file(), f"missing fixture {path}"
        raw = path.read_bytes()
        assert raw == canonical_fixture_bytes(fixture_id)
        assert raw == canonical_fixture_bytes(fixture_id)


def test_fixture_classes_are_materially_different() -> None:
    """Given all three fixtures, Then shot materiality differs per class."""
    speaker = load_fixture("speaker-broll")
    visual = load_fixture("visual-first")
    screen = load_fixture("screen-product")
    speaker_broll = [s for s in speaker.shots if s.editorial.role == "broll"]
    speaker_talk = [s for s in speaker.shots if s.transcript_segments]
    visual_heroes = [s for s in visual.shots if s.editorial.select_potential == "high"]
    screen_texts = [s for s in screen.shots if s.visible_texts]
    screen_products = [s for s in screen.shots if s.product_refs]
    assert len(speaker_broll) >= 2
    assert len(speaker_talk) >= 5
    assert len(visual_heroes) >= 3
    assert all(hero.transcript_segments is None for hero in visual_heroes)
    assert len(visual.shots) > len(visual_heroes)
    assert screen_texts
    assert screen_products


def test_universal_pass_routes_all_material_and_budget_has_trigger_reasons(
    tmp_path: Path,
) -> None:
    """Given each fixture chain, Then every shot is triaged (universal pass)
    and the budget artifact records explicit trigger reasons within SLO."""
    for fixture_id in FIXTURE_IDS:
        chain = run_chain(
            load_fixture(fixture_id),
            policy=GATE_POLICY,
            index_path=tmp_path / f"{fixture_id}.duckdb",
        )
        assert chain.plan.budget.universal_pass.shots_count == len(chain.reconciled.shots)
        assert len(chain.plan.triage) == len(chain.reconciled.shots)
        windows = chain.plan.deep_review_windows
        assert windows
        assert all(w.trigger_reason in TRIGGER_REASONS for w in windows)
        budget = chain.plan.budget
        assert budget.source_duration_seconds > 0
        assert budget.reviewed_seconds / budget.source_duration_seconds <= 0.250001


def test_nonverbal_valuable_moment_retrievable_without_transcript(
    tmp_path: Path,
) -> None:
    """Given the visual-first fixture, When queried via v2, Then a visually
    driven high-value moment surfaces with NO transcript attached."""
    chain = run_chain(
        load_fixture("visual-first"),
        policy=GATE_POLICY,
        index_path=tmp_path / "visual-first.duckdb",
    )
    with MediaQueryApiV2.open(chain.index_path) as api:
        moments = api.best_moments(
            BestMomentsRequest(
                select_potentials=("high",), pagination=V2Pagination(limit=50, offset=0)
            )
        )
        assert moments.total >= 3
        for row in moments.rows:
            detail = api.shot_detail(ShotDetailRequest(shot_id=row.shot_id))
            assert detail.shot.transcript_segments is None
            assert detail.shot.visual.shot_size
        reviews = api.moment_reviews(
            MomentReviewsRequest(
                span=FrameSpan(start_frame=150, end_frame=510),
                pagination=V2Pagination(limit=50, offset=0),
            )
        )
        assert reviews.total >= 3
        assert all(row.overall_confidence > 0 for row in reviews.rows)


def test_broll_candidates_retrievable_by_meaning_ja_and_en(tmp_path: Path) -> None:
    """Given the speaker-broll fixture, When searched semantically in Japanese
    AND English, Then B-roll shots are hit by meaning."""
    chain = run_chain(
        load_fixture("speaker-broll"),
        policy=GATE_POLICY,
        index_path=tmp_path / "speaker-broll.duckdb",
    )
    broll_ids = {
        str(shot.shot_id) for shot in chain.reconciled.shots if shot.editorial.role == "broll"
    }
    assert len(broll_ids) >= 2
    with MediaQueryApiV2.open(chain.index_path) as api:
        en = api.semantic_shot_search(
            SemanticSearchRequest(text_query="b-roll", pagination=V2Pagination(limit=50, offset=0))
        )
        ja = api.semantic_shot_search(
            SemanticSearchRequest(text_query="渋谷", pagination=V2Pagination(limit=50, offset=0))
        )
        transcript = api.transcript_range(
            TranscriptRangeRequest(
                source_id="src-cam-a",
                span=FrameSpan(start_frame=0, end_frame=180),
                pagination=V2Pagination(limit=50, offset=0),
            )
        )
    assert {row.shot_id for row in en.rows} & broll_ids
    assert {row.shot_id for row in ja.rows} & broll_ids
    assert transcript.total >= 1


def test_mcp_internal_db_is_not_the_authoritative_artifact() -> None:
    """Given the committed fixtures, Then canonical bytes exist on disk, no
    private/vendor path leaks into them, and sources are synthetic URIs."""
    for fixture_id in FIXTURE_IDS:
        raw = (FIXTURE_ROOT / fixture_id / "artifact.json").read_bytes()
        assert b"private/" not in raw
        assert b"vendor/" not in raw
        assert b"/Users/" not in raw
        artifact = MediaIntelligenceArtifact.model_validate_json(raw)
        for source in artifact.sources:
            assert str(source.path).startswith("synthetic://")
        assert artifact.shots


def test_index_rows_carry_canonical_artifact_lineage(tmp_path: Path) -> None:
    """Given a built chain, Then every queried row's artifact_sha equals the
    canonical artifact content sha (pipeline reads only canonical bytes)."""
    chain = run_chain(
        load_fixture("speaker-broll"),
        policy=GATE_POLICY,
        index_path=tmp_path / "lineage.duckdb",
    )
    expected_sha = artifact_content_sha(chain.reconciled)
    with MediaQueryApiV2.open(chain.index_path) as api:
        shots = api.shots(
            ShotsRequest(
                span=FrameSpan(start_frame=0, end_frame=10_000),
                pagination=V2Pagination(limit=50, offset=0),
            )
        )
    assert shots.total == len(chain.reconciled.shots)
    assert all(row.artifact_sha == expected_sha for row in shots.rows)


def test_recall_audit_runs_on_every_fixture(tmp_path: Path) -> None:
    """Given each fixture chain, Then the sentinel drew a sample, reviewed
    it, and produced a report (Gate may fail when this cannot run)."""
    for fixture_id in FIXTURE_IDS:
        chain = run_chain(
            load_fixture(fixture_id),
            policy=GATE_POLICY,
            index_path=tmp_path / f"recall-{fixture_id}.duckdb",
        )
        report = chain.recall_report
        assert report is not None
        assert report.sampled_count >= 1
        assert report.reviewed_count >= 1
        assert report.miss_rate >= 0
        assert report.degradation_signal in ("ok", "degraded")


def test_benchmark_counters_deterministic_and_reanalysis_dependent_only(
    tmp_path: Path,
) -> None:
    """Given the benchmark on every fixture with an injected monotonic, Then
    counters are byte-identical across double runs and a one-shot correction
    recomputes only dependent work (reused reviews count as cache hits)."""
    for fixture_id in FIXTURE_IDS:
        artifact = load_fixture(fixture_id)
        first = run_benchmark(
            artifact,
            policy=GATE_POLICY,
            fixture_id=fixture_id,
            index_path=tmp_path / f"bench1-{fixture_id}.duckdb",
            monotonic=_fake_monotonic(),
        )
        second = run_benchmark(
            artifact,
            policy=GATE_POLICY,
            fixture_id=fixture_id,
            index_path=tmp_path / f"bench2-{fixture_id}.duckdb",
            monotonic=_fake_monotonic(),
        )
        assert first.model_dump_json() == second.model_dump_json()
        reanalysis = first.reanalysis_after_correction
        assert reanalysis.mode == "dependent_only"
        assert reanalysis.reused_reviews > 0
        assert reanalysis.recomputed_windows < first.stages.deep_review.reviews_executed
        assert first.cache_hits == reanalysis.reused_reviews
        assert first.token_or_frame_counters.tokens == 0
        assert first.deep_review_ratio <= 0.250001
        assert first.stages.deep_review.reviews_executed == len(first.plan_unique_spans)
        assert len(first.plan_unique_spans) <= first.stages.triage.windows_planned


def test_gate_summary_matches_fresh_benchmark_runs() -> None:
    """Given the committed gate summary, When the benchmark is re-run, Then
    the deterministic counters match (a drifted summary fails the gate)."""
    summary_path = CAPABILITIES_ROOT / "gate-v43-1" / "gate-summary.json"
    assert summary_path.is_file()
    runs = [
        run_benchmark(
            load_fixture(fixture_id),
            policy=GATE_POLICY,
            fixture_id=fixture_id,
            monotonic=_fake_monotonic(),
        )
        for fixture_id in FIXTURE_IDS
    ]
    committed = json.loads(summary_path.read_text())
    assert committed["gate"] == "V43-1"
    assert committed["fixtures"] == deterministic_summary(runs)


def test_backend_transition_recorded_after_gate() -> None:
    """Given the committed transition record, Then it documents the
    analysis_backend legacy_local → hybrid switch with gate V43-1 evidence."""
    record_path = CAPABILITIES_ROOT / "gate-v43-1" / "backend-transition.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text())
    assert record["from"] == "legacy_local"
    assert record["to"] == "hybrid"
    assert record["gate"] == "V43-1"
    assert record["switched_key"] == "analysis_backend"
    assert record["evidence_refs"]
    assert "scheduler" in record["deep_review"]


def test_aggregate_report_shape_is_deterministic() -> None:
    """Given aggregate_runs, Then the report is fixture-sorted and carries
    the real-duration measurement note for tasks 30/43/52."""
    runs = [
        run_benchmark(
            load_fixture(fixture_id),
            policy=GATE_POLICY,
            fixture_id=fixture_id,
            monotonic=_fake_monotonic(),
        )
        for fixture_id in FIXTURE_IDS
    ]
    report = aggregate_runs(runs)
    assert report["schema_version"] == "benchmark-report-v1"
    fixtures = report["fixtures"]
    assert isinstance(fixtures, dict)
    assert tuple(fixtures) == tuple(sorted(FIXTURE_IDS))
    notes = report["notes"]
    assert isinstance(notes, tuple)
    assert any("30/43/52" in note for note in notes)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
