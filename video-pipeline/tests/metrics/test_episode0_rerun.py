"""Episode-0 phase rerun harness (task 30) — TDD, synthetic data only.

Proves the ``episode0 run --phase editorial-v2`` harness end-to-end on the
task-28 synthetic W3 episode (NO real footage, NO LLM, llm_call=None):
(a) missing/unmeasured baseline → BLOCKED with an explicit Gate V43-0.5
    unmet message and operator escalation (non-zero exit);
(b) baseline present → synthetic run completes and writes report.json +
    gate-check.json;
(c) the editorial_contract flag switch/restore are BOTH logged even when a
    pipeline stage fails (try/finally proof);
(d) compare --a baseline --b <run> emits a delta with the editorial
    additions (kept non-speech, evidence coverage, hallucination count,
    taste citations); a missing comparison side is 比較不能 display-only;
(e) Gate V43-2 machine checklist passes on the synthetic run;
(f) hallucination injection → gate fails.

Path-mode compare (two episode0-report-v1 files) keeps T5 behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.cli.episode0 import main
from services.config.backends import BackendsConfig, load_backends
from services.editorial_v2.model_provider import EditorialRuntimeV1
from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.episode0_baseline import (
    Episode0BaselineLogV1,
    Episode0SourceManifest,
    generate_report,
)
from services.metrics.episode0_gate_v43_2 import (
    Episode0RerunReportV1,
    GateEvidenceV1,
    check_gate,
)
from services.preview.errors import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools, run_bounded
from tests.editorial_v2.fixtures.three_pass_fixture import (
    make_brief,
    make_episode_artifact,
)

if TYPE_CHECKING:
    from services.media_intelligence.models import MediaIntelligenceArtifact


PHASE_0C_LOCK = Path("config/toolchains/phase-0c-v1.json")
GATE_REQUIREMENTS = {
    "story-plan-generated",
    "non-speech-candidates-present",
    "domain-scoped-taste",
    "no-invented-spans-ids",
    "review-event-corrections",
}
_HEURISTIC_RUNTIME = EditorialRuntimeV1(
    schema_version="editorial-runtime-v1",
    mode="heuristic_diagnostic",
    director_pin_path="config/toolchains/pins/editorial-director-v2.json",
    moment_review_pin_path="config/toolchains/pins/moment-review-multimodal.json",
    review_interpreter_pin_path="config/toolchains/pins/review-interpreter.json",
)


def _baseline_log_dict() -> dict[str, object]:
    return {
        "schema_version": "episode0-baseline-v1",
        "active_human_time_minutes": 42.5,
        "ttfrp_minutes": 10.0,
        "wall_clock_minutes": 60.0,
        "manual_resolve_minutes": 5.0,
        "wrong_keep_remove": [{"ts": "00:01:23", "note": "kept filler"}],
        "missed_moments": [{"ts": "00:02:00", "note": "missed reaction"}],
        "finishing_deficits": [{"ts": "00:03:00", "note": "color off"}],
        "interruption_points": [{"ts": "00:04:00", "note": "interrupted"}],
        "publishability": {
            "publishable": False,
            "comment": "needs finishing",
            "best_ts": "00:01:00",
            "worst_ts": "00:03:00",
        },
    }


def _write_baseline(runs_root: Path) -> Path:
    log = Episode0BaselineLogV1.model_validate(_baseline_log_dict())
    manifest = Episode0SourceManifest(
        schema_version="episode0-source-manifest-v1",
        video_path="unused.mp4",
        sha256="a" * 64,
        size_bytes=16,
        duration_seconds=None,
        duration_probed=False,
    )
    return generate_report(log, manifest, run_id="baseline", runs_root=runs_root)


def _write_backends(tmp_path: Path) -> Path:
    path = tmp_path / "backends.json"
    config = BackendsConfig(
        schema_version="backends-v1",
        execution_backend="legacy_direct",
        analysis_backend="legacy_local",
        editorial_contract="phase1_v1",
    )
    atomic_write(path, canonical_model_bytes(config))
    return path


_SHOT_SPANS = {
    "shot-a": (0, 99),
    "shot-b": (99, 198),
    "shot-c": (198, 297),
    "shot-d": (297, 396),
    "shot-f": (396, 495),
    "shot-e": (495, 594),
    "shot-g": (594, 603),
}
_SEGMENT_SPANS = {"tr-a1": (9, 90), "tr-c1": (201, 294), "tr-e1": (498, 591)}


def _preview_compatible_artifact() -> MediaIntelligenceArtifact:
    """Shared fixture episode re-spanned onto a 3-frame grid.

    Two preview contracts drive the grid: (a) SRT sidecar cues require record
    spans on exact millisecond bounds at 30/1 — every frame index (shot spans,
    transcript bounds, and therefore placement offsets) must be divisible by 3;
    (b) the pinned renderer's drift verifier is exact on 3-divisible concat
    segment lengths. Decisions are untouched — the heuristic keys on
    descriptions/potentials, never on frame bounds.
    """

    artifact = make_episode_artifact()
    shots: list[object] = []
    for shot in artifact.shots:
        start, end = _SHOT_SPANS[shot.shot_id]
        segments = shot.transcript_segments
        new_segments = (
            None
            if not segments
            else tuple(
                segment.model_copy(
                    update={
                        "start_frame": _SEGMENT_SPANS[segment.segment_id][0],
                        "end_frame": _SEGMENT_SPANS[segment.segment_id][1],
                    }
                )
                for segment in segments
            )
        )
        editorial = shot.editorial.model_copy(
            update={
                "best_moment": shot.editorial.best_moment.model_copy(
                    update={"frame": start + (end - start) // 2}
                )
            }
        )
        shots.append(
            shot.model_copy(
                update={
                    "source_span": type(shot.source_span)(start_frame=start, end_frame=end),
                    "transcript_segments": new_segments,
                    "editorial": editorial,
                }
            )
        )
    sources = (artifact.sources[0].model_copy(update={"duration_frames": 603}),)
    return artifact.model_copy(update={"shots": tuple(shots), "sources": sources})


class _Workspace:
    """Synthetic episode workspace (tmp only; real reference episodes untouched)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_root = root / "runs"
        self.brief_path = root / "brief.json"
        self.mi_path = root / "media-intelligence.json"
        # NO-LLM harness by design: the shipped default is production_model
        # (BLOCKS without credentials), so these runs opt into heuristic mode.
        self.editorial_runtime_path = root / "editorial-runtime.json"
        atomic_write(
            self.brief_path, canonical_model_bytes(make_brief())
        )
        atomic_write(self.mi_path, canonical_model_bytes(_preview_compatible_artifact()))
        atomic_write(
            self.editorial_runtime_path,
            canonical_model_bytes(_HEURISTIC_RUNTIME),
        )

    def run_argv(self, *, run_id: str, backends: Path, extra: tuple[str, ...] = ()) -> list[str]:
        return [
            "run",
            "--phase",
            "editorial-v2",
            "--episode",
            "synthetic-01",
            "--episode-root",
            str(self.root),
            "--brief",
            str(self.brief_path),
            "--mi-artifact",
            str(self.mi_path),
            "--backends",
            str(backends),
            "--editorial-runtime",
            str(self.editorial_runtime_path),
            "--run-id",
            run_id,
            *extra,
        ]

    @property
    def run_dir(self) -> Path:
        return self.runs_root / "unit-editorial-v2"


@pytest.fixture
def workspace(tmp_path: Path) -> _Workspace:
    return _Workspace(tmp_path)


@pytest.fixture
def backends(tmp_path: Path) -> Path:
    return _write_backends(tmp_path)


@pytest.fixture(scope="session")
def preview_tools() -> PinnedTools:
    try:
        return load_pinned_tools(PHASE_0C_LOCK)
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="session")
def synthetic_media(preview_tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Synthetic Edit Source media via ffmpeg lavfi (testsrc2 + sine, 30/1, 48 kHz)."""

    media = tmp_path_factory.mktemp("ep0-rerun-media") / "synthetic-source.mov"
    run_bounded(
        [
            str(preview_tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=600:sample_rate=48000",
            "-t",
            "21",
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "30000",
            "-r",
            "30",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(media),
        ]
    )
    return media


def _run_events(run_dir: Path) -> list[dict[str, object]]:
    lines = (run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# -------------------------------------------------- (a) BLOCKED preconditions


def test_run_is_blocked_when_baseline_report_missing(
    workspace: _Workspace, backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: no baseline report; When: run; Then: BLOCKED (Gate V43-0.5 unmet)."""

    rc = main(workspace.run_argv(run_id="blocked-run", backends=backends))
    assert rc != 0
    stderr = capsys.readouterr().err
    assert "Gate V43-0.5" in stderr
    assert "BLOCKED" in stderr
    assert "operator" in stderr.casefold()
    # the flag was never switched
    assert load_backends(backends).editorial_contract == "phase1_v1"


def test_run_is_blocked_when_baseline_aht_is_null(
    workspace: _Workspace, backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: baseline report whose active_human_time_minutes is null; Then: BLOCKED."""

    baseline_dir = workspace.runs_root / "baseline"
    baseline_dir.mkdir(parents=True)
    payload = dict.fromkeys(
        ("schema_version", "run_id", "manifest", "log"),
    )
    payload["log"] = {"active_human_time_minutes": None}
    (baseline_dir / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    rc = main(workspace.run_argv(run_id="blocked-aht", backends=backends))
    assert rc != 0
    stderr = capsys.readouterr().err
    assert "Gate V43-0.5" in stderr
    assert "active_human_time_minutes" in stderr


# --------------------------------------------- (b) synthetic run end-to-end


def test_synthetic_run_completes_with_report_and_gate_check(
    workspace: _Workspace, backends: Path
) -> None:
    """Given: measured baseline + synthetic brief/MI artifact; When: run; Then:
    report.json + gate-check.json generated, gate passed, operator fields null."""

    _write_baseline(workspace.runs_root)
    rc = main(workspace.run_argv(run_id="unit-editorial-v2", backends=backends))
    assert rc == 0

    report_path = workspace.run_dir / "report.json"
    assert report_path.is_file()
    report = Episode0RerunReportV1.model_validate(json.loads(report_path.read_bytes()))
    assert report.phase == "editorial-v2"
    assert report.operator.active_human_time_minutes is None
    assert report.validation.hallucination_count == 0
    assert report.review_correction.exercised is True
    assert report.commit.version == 2
    assert report.editorial.kept_non_speech_count >= 1
    assert report.editorial.evidence_coverage_rate == 1.0
    assert report.taste.explicitly_absent is True
    assert report.preview.skipped_reason is not None

    gate = json.loads((workspace.run_dir / "gate-check.json").read_bytes())
    assert gate["passed"] is True
    assert {c["requirement"] for c in gate["criteria"]} == GATE_REQUIREMENTS
    assert all(c["status"] == "pass" for c in gate["criteria"])

    # flag restored + both transitions logged
    assert load_backends(backends).editorial_contract == "phase1_v1"
    events = _run_events(workspace.run_dir)
    switched = [e for e in events if e["event"] == "flag_switched"]
    restored = [e for e in events if e["event"] == "flag_restored"]
    assert len(switched) == 1
    assert switched[0]["to"] == "multimodal_v2"
    assert len(restored) == 1
    assert restored[0]["to"] == "phase1_v1"


def test_synthetic_run_with_media_renders_editorial_preview(
    workspace: _Workspace, backends: Path, synthetic_media: Path
) -> None:
    """Given: synthetic source media; When: run; Then: editorial preview rendered + traced."""

    _write_baseline(workspace.runs_root)
    rc = main(
        workspace.run_argv(
            run_id="unit-editorial-v2",
            backends=backends,
            extra=("--source-media", str(synthetic_media)),
        )
    )
    assert rc == 0
    report = Episode0RerunReportV1.model_validate(
        json.loads((workspace.run_dir / "report.json").read_bytes())
    )
    assert report.preview.skipped_reason is None
    assert report.preview.total_record_frames == 297
    assert report.preview.preview_path is not None
    assert Path(report.preview.preview_path).is_file()
    assert Path(report.preview.preview_path).with_suffix(".trace.json").is_file()


# --------------------------------------------- (c) flag restore on failure


def test_flag_switch_and_restore_logged_even_when_a_stage_fails(
    workspace: _Workspace,
    backends: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Given: an induced compile-stage failure; Then: non-zero exit, BOTH flag
    transitions logged, backends file restored to phase1_v1."""

    _write_baseline(workspace.runs_root)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("induced compile failure")

    monkeypatch.setattr("services.cli.episode0._stage_compile", boom)
    rc = main(workspace.run_argv(run_id="unit-editorial-v2", backends=backends))
    assert rc != 0
    assert "run_failed" in capsys.readouterr().err

    assert load_backends(backends).editorial_contract == "phase1_v1"
    events = _run_events(workspace.run_dir)
    assert [e["event"] for e in events] == ["flag_switched", "flag_restored"]
    assert events[0]["to"] == "multimodal_v2"
    assert events[1]["to"] == "phase1_v1"


# --------------------------------------------- (d) compare delta


def test_compare_baseline_vs_rerun_emits_editorial_delta(
    workspace: _Workspace, backends: Path, tmp_path: Path
) -> None:
    """Given: baseline + completed rerun; When: compare --a baseline --b <run>;
    Then: delta carries T5 fields (b pending) + editorial additions."""

    _write_baseline(workspace.runs_root)
    assert main(workspace.run_argv(run_id="unit-editorial-v2", backends=backends)) == 0

    out = tmp_path / "delta.json"
    rc = main(
        [
            "compare",
            "--a",
            "baseline",
            "--b",
            "unit-editorial-v2",
            "--runs-root",
            str(workspace.runs_root),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    delta: dict[str, object] = json.loads(out.read_bytes())
    assert delta["active_human_time_minutes"] == {"a": 42.5, "b": None, "delta": None}
    assert delta["operator_measurement_pending"] is True
    editorial = delta["editorial"]
    assert isinstance(editorial, dict)
    assert editorial["kept_non_speech_count"] >= 1
    assert editorial["evidence_coverage_rate"] == 1.0
    assert editorial["hallucination_count"] == 0
    assert editorial["taste_citation_count"] == 0
    assert delta["hallucination_must_be_zero"] == {"count": 0, "ok": True}


def test_compare_with_missing_run_reports_comparison_unavailable(
    workspace: _Workspace, tmp_path: Path
) -> None:
    """Given: baseline but the b-side run is missing; Then: 比較不能 display only (exit 0)."""

    _write_baseline(workspace.runs_root)
    out = tmp_path / "delta.json"
    rc = main(
        [
            "compare",
            "--a",
            "baseline",
            "--b",
            "no-such-run",
            "--runs-root",
            str(workspace.runs_root),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    payload: dict[str, object] = json.loads(out.read_bytes())
    assert payload["comparison"] == "unavailable"
    assert isinstance(payload["reason"], str)
    assert "no-such-run" in str(payload["reason"])


def test_compare_path_mode_keeps_t5_delta_behavior(tmp_path: Path) -> None:
    """Given: two episode0-report-v1 files (T5 shape); When: compare by path;
    Then: T5 delta unchanged (regression guard)."""

    manifest = Episode0SourceManifest(
        schema_version="episode0-source-manifest-v1",
        video_path="unused.mp4",
        sha256="a" * 64,
        size_bytes=16,
        duration_seconds=None,
        duration_probed=False,
    )
    log_a = Episode0BaselineLogV1.model_validate(
        {**_baseline_log_dict(), "active_human_time_minutes": 30.0}
    )
    log_b = Episode0BaselineLogV1.model_validate(
        {**_baseline_log_dict(), "active_human_time_minutes": 45.0}
    )
    path_a = generate_report(log_a, manifest, run_id="run-a", runs_root=tmp_path / "runs")
    path_b = generate_report(log_b, manifest, run_id="run-b", runs_root=tmp_path / "runs")
    rc = main(["compare", "--a", str(path_a), "--b", str(path_b)])
    assert rc == 0


# --------------------------------------------- (e,f) gate checklist


def test_gate_check_passes_on_synthetic_run(workspace: _Workspace, backends: Path) -> None:
    """Given: the synthetic run's report + evidence; Then: all 5 criteria pass."""

    _write_baseline(workspace.runs_root)
    assert main(workspace.run_argv(run_id="unit-editorial-v2", backends=backends)) == 0
    gate = json.loads((workspace.run_dir / "gate-check.json").read_bytes())
    assert gate["passed"] is True
    by_requirement = {c["requirement"]: c for c in gate["criteria"]}
    assert set(by_requirement) == GATE_REQUIREMENTS
    assert by_requirement["review-event-corrections"]["status"] == "pass"
    assert by_requirement["domain-scoped-taste"]["status"] == "pass"


def test_gate_fails_when_hallucination_injected(workspace: _Workspace, backends: Path) -> None:
    """Given: a report/evidence pair naming an invented ref; Then: gate fails."""

    _write_baseline(workspace.runs_root)
    assert main(workspace.run_argv(run_id="unit-editorial-v2", backends=backends)) == 0
    report = Episode0RerunReportV1.model_validate(
        json.loads((workspace.run_dir / "report.json").read_bytes())
    )
    poisoned = report.model_copy(
        update={
            "validation": report.validation.model_copy(
                update={
                    "hallucination_count": 1,
                    "hallucinated_refs": ("shot-ghost-42",),
                }
            )
        }
    )
    evidence = GateEvidenceV1(
        story_plan_block_count=report.editorial.story_plan_block_count,
        selection_candidate_count=report.editorial.selection_candidate_count,
        non_speech_candidate_count=report.editorial.non_speech_candidate_count,
        review_committed_event_count=2,
        hallucinated_refs=("shot-ghost-42",),
    )
    gate = check_gate(poisoned, evidence)
    assert gate.passed is False
    failed = [c for c in gate.criteria if c.status == "fail"]
    assert [c.requirement for c in failed] == ["no-invented-spans-ids"]
    assert "shot-ghost-42" in failed[0].detail


# --------------------------------------------- malformed input probes


def test_run_refuses_unapproved_brief(
    workspace: _Workspace, backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: a draft (unapproved) brief; Then: typed refusal, non-zero, flag restored."""

    _write_baseline(workspace.runs_root)
    draft = make_brief().model_copy(update={"status": "draft", "approval_ref": None})
    atomic_write(workspace.brief_path, canonical_model_bytes(draft))
    rc = main(workspace.run_argv(run_id="bad-brief", backends=backends))
    assert rc != 0
    assert "not-approved" in capsys.readouterr().err
    assert load_backends(backends).editorial_contract == "phase1_v1"


def test_run_refuses_malformed_mi_artifact(
    workspace: _Workspace, backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given: a non-JSON media-intelligence artifact; Then: typed refusal, non-zero."""

    _write_baseline(workspace.runs_root)
    workspace.mi_path.write_bytes(b"not json at all")
    rc = main(workspace.run_argv(run_id="bad-mi", backends=backends))
    assert rc != 0
    assert "run_failed" in capsys.readouterr().err
    assert load_backends(backends).editorial_contract == "phase1_v1"
