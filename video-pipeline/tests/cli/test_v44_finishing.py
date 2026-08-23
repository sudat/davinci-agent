"""Task 13: live finishing + QC + publishability protocol (Tier B, offline).

The pinned-ffmpeg fixture renders are REAL media; the MCP executor is the
fake replay (readback-verified, no server) — the mcp_execution fake-executor
test precedent. Every blocked path asserts a typed non-zero refusal, never
a misleading success. The live-executor refusal is proven at the seam
(episode0's bounded probe raises before any execution).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest

from services.cli import _v44_finishing_run, v44_finishing
from services.cli._v44_finishing_build import resolve_review_store
from services.cli._v44_finishing_report import TimeLogLineV1
from services.cli.bundle import ReviewTarget, assemble_real_bundle, save_bundle
from services.cli.episode0 import Episode0BlockedError
from services.cli.project import init_review_store, plan_sha256
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.final_review.publishability import PublishabilityReviewV1
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.errors import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools
from services.production_kit.preview import KitDomainSelectionV1, KitSelectionRecordV1
from services.qc.policy_build import build_policy

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[2]
RATE = RationalFrameRate(num=30, den=1)
EPISODE_ID = "ep-v44-finishing"
CUE_1 = "今日はDaVinci Resolveの使い方を紹介します"
CUE_2 = "再生と編集の違いに注意してください"
SEVEN_DOMAINS = {
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
}


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.cli.v44_finishing", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
        timeout=600,
    )


def _seed_plan(text_1: str = CUE_1, text_2: str = CUE_2) -> EditPlan0C:
    def item(
        item_id: str,
        kind: str,
        start: int,
        end: int,
        subtitle_text: str | None = None,
    ) -> EditPlanItem0C:
        track_index = {"video": 1, "audio": 2, "subtitle": 3}[kind]
        return EditPlanItem0C(
            item_id=item_id,
            kind=cast('Literal["video", "audio", "subtitle"]', kind),
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=track_index,
            subtitle_text=subtitle_text,
        )

    return EditPlan0C(
        artifact_id="edit-plan-v44-finishing-fixture",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="v44-finishing-test", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=90),
            items=(
                item("v1", "video", 0, 60),
                item("v2", "video", 60, 90),
                item("a1", "audio", 0, 90),
                item("s1", "subtitle", 15, 45, subtitle_text=text_1),
                item("s2", "subtitle", 65, 86, subtitle_text=text_2),
            ),
        ),
    )


def _mirror_review_store(episode_root: Path, run_dir: Path) -> None:
    """The T7/T9 mirror split: events+seal -> review/, the rest -> review/store/."""

    source = run_dir / "review-store"
    log_target = episode_root / "review"
    store_target = episode_root / "review" / "store"
    log_target.mkdir(parents=True, exist_ok=True)
    store_target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name in ("events.jsonl", "events.jsonl.seal"):
            shutil.copyfile(entry, log_target / entry.name)
        else:
            shutil.copyfile(entry, store_target / entry.name)


def _kit_record(
    *entries: tuple[str, str | None, str | None],
) -> KitSelectionRecordV1:
    return KitSelectionRecordV1(
        episode_id=EPISODE_ID,
        entries=tuple(
            KitDomainSelectionV1(
                domain=domain,
                recipe_id=recipe,
                semantic_intent=intent,
                recorded_at="2026-08-23T00:00:00Z",
            )
            for domain, recipe, intent in entries
        ),
    )


FULL_SELECTIONS = (
    ("subtitle", "subtitle/default", "subtitle_track"),
    ("audio", "audio/dialogue-chain", "voice_isolation"),
    ("color", "color/channel-look", "color_look"),
)


def _workspace(tmp_path: Path, mezzanine: Path, record: KitSelectionRecordV1) -> Path:
    episode_root = tmp_path / EPISODE_ID
    run_dir = episode_root / "run"
    (run_dir / "media").mkdir(parents=True)
    shutil.copyfile(mezzanine, run_dir / "media" / "edit-source.mov")
    mezz_sha = sha256_file(run_dir / "media" / "edit-source.mov")
    init_review_store(_seed_plan(), run_dir / "review-store")
    _mirror_review_store(episode_root, run_dir)
    seed = _seed_plan()
    save_bundle(
        assemble_real_bundle(
            episode_id=EPISODE_ID,
            eligibility_status="supported",
            mezzanine_sha256=mezz_sha,
            edit_source_world_sha256=mezz_sha,
            episode_manifest_sha256=mezz_sha,
            policy_sha256=mezz_sha,
            target=ReviewTarget(
                plan_version="v1",
                plan_sha256=plan_sha256(seed),
                ir_sha256=sha256_file(run_dir / "review-store" / "ir-v1.json"),
                preview_dir="preview-v1",
                preview_sha256=mezz_sha,
                trace_sha256=mezz_sha,
            ),
        ),
        run_dir / "review-bundle.json",
    )
    atomic_write(episode_root / "kit-selections.json", canonical_model_bytes(record))
    return episode_root


@pytest.fixture(scope="module")
def tools() -> Iterator[PinnedTools]:
    try:
        yield load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="module")
def fixture_clip(tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real mp4: lavfi testsrc2 + sine 48 kHz, h264_videotoolbox/aac."""

    media = tmp_path_factory.mktemp("v44-finishing-fixture") / "clip.mp4"
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
            "testsrc2=size=320x180:rate=30:duration=3",
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
def episode_root(fixture_clip: Path, tmp_path: Path) -> Path:
    return _workspace(tmp_path, fixture_clip, _kit_record(*FULL_SELECTIONS))


def test_run_fake_without_policy_blocks_on_delivery_qc(episode_root: Path) -> None:
    """Honest blocked: no QC policy ⇒ verdict null-with-reason ⇒ delivery_qc blocked."""

    result = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert result.returncode == 1, result.stderr
    assert "delivery_qc" in result.stderr

    report = json.loads((episode_root / "finishing" / "finishing-run.json").read_bytes())
    assert report["schema_version"] == "v44-finishing-run-v1"
    assert report["technical_qc"]["verdict"] is None
    assert report["technical_qc"]["reason"]
    assert report["domain_statuses"]["delivery_qc"] == "blocked"
    assert (
        "did not pass" in report["domain_justifications"]["delivery_qc"]
    )
    assert (episode_root / "finishing" / "final-preview" / "preview.mp4").is_file()


def test_run_fake_with_policy_happy_path_all_domains(episode_root: Path) -> None:
    """Full happy path: compile + fake execution + real render + real QC."""

    first = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert first.returncode == 1  # no policy yet — honest blocked (previous test)
    policy_path = episode_root / "finishing" / "qc-policy.json"
    policy = build_policy(episode_root / "finishing" / "final-preview" / "preview.mp4")
    atomic_write(policy_path, canonical_model_bytes(policy))

    result = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake",
         "--qc-policy", str(policy_path)]
    )
    assert result.returncode == 0, result.stderr

    report = json.loads((episode_root / "finishing" / "finishing-run.json").read_bytes())
    assert set(report["domain_statuses"]) == SEVEN_DOMAINS
    for domain, status in report["domain_statuses"].items():
        assert status in {"applied", "intentionally_not_needed"}, (domain, status)
        if status == "intentionally_not_needed":
            assert report["domain_justifications"][domain]
    assert report["gate_decision"] == "pass"
    assert report["blocked_domains"] == []
    assert report["plan_compiled"] is True
    assert report["execution_outcome"] == "completed"
    assert report["failed_step_count"] == 0
    # Failed MCP capabilities routed to fallback rungs automatically.
    assert report["fallback_rung_count"] >= 1
    # Executor identity + pins/lineage recorded (no misleading success).
    assert report["executor"] == "fake"
    assert "fake" in report["executor_note"]
    assert report["director_model_id"] == "gpt-5.6-sol"
    assert report["analysis_provider"].startswith("whisper-cpp-cli:")
    # Real QC verdict attached (never fabricated).
    assert report["technical_qc"]["verdict"] == "passed"
    assert report["technical_qc"]["report_path"].endswith("qc-report.json")
    assert report["editorial_qc"]["candidate_count"] >= 0
    assert Path(report["editorial_qc"]["report_path"]).is_file()
    assert report["review_head_version"] == 1
    assert report["wall_clock_seconds"] >= 0.0
    assert {entry["domain"] for entry in report["kit_selections"]} == {
        "audio", "color", "subtitle"
    }
    preview = Path(report["final_preview_path"])
    assert preview.is_file()
    assert report["final_preview_sha256"] == sha256_file(preview)
    # Sub-record artifacts exist for the gate evidence (T16).
    for name in (
        "subtitle-plan.json",
        "audio-plan.json",
        "color-plan.json",
        "mcp-execution-plan.json",
        "mcp-run-report.json",
        "quality-domain-report.json",
        "editorial-qc-report.json",
    ):
        assert (episode_root / "finishing" / name).is_file(), name


def test_missing_audio_selection_blocks_audio_domain(
    fixture_clip: Path, tmp_path: Path
) -> None:
    """Operator chose どちらも不要 for audio ⇒ no plan ⇒ existing blocked semantics."""

    record = _kit_record(
        ("subtitle", "subtitle/default", "subtitle_track"),
        ("audio", None, None),
        ("color", "color/channel-look", "color_look"),
    )
    episode_root = _workspace(tmp_path, fixture_clip, record)
    result = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert result.returncode == 1, result.stderr
    assert "audio_finishing" in result.stderr

    report = json.loads((episode_root / "finishing" / "finishing-run.json").read_bytes())
    assert report["plan_compiled"] is False
    assert report["execution_outcome"] is None
    assert report["domain_statuses"]["audio_finishing"] == "blocked"
    assert report["domain_justifications"]["audio_finishing"] == (
        "no explicit audio finishing plan exists (plan/result is required)"
    )
    assert "audio_finishing" in report["blocked_domains"]


def test_missing_kit_selections_is_typed_blocked(episode_root: Path) -> None:
    (episode_root / "kit-selections.json").unlink()
    result = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert result.returncode == 1
    assert "kit-selections-missing" in result.stderr
    assert not (episode_root / "finishing" / "finishing-run.json").is_file()


def test_live_executor_without_server_blocks_at_the_seam(
    episode_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(pin_path: Path) -> object:
        raise Episode0BlockedError(
            "mcp-server-unreachable",
            f"stubbed probe: pinned MCP server not reachable ({pin_path})",
        )

    monkeypatch.setattr(_v44_finishing_run, "_probe_live_executor", refuse)
    code = v44_finishing.main(
        ["run", "--episode-root", str(episode_root), "--executor", "live"]
    )
    assert code == v44_finishing.EXIT_BLOCKED
    stderr = capsys.readouterr().err
    assert "mcp-server-unreachable" in stderr
    # No report: the run was refused before any execution.
    assert not (episode_root / "finishing" / "finishing-run.json").is_file()


def test_record_publishability_refuses_unfinished_state(tmp_path: Path) -> None:
    episode_root = tmp_path / "ep-unfinished"
    episode_root.mkdir()
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable"]
    )
    assert result.returncode == 1
    assert "finishing-report-missing" in result.stderr


def test_record_publishability_round_trip(episode_root: Path) -> None:
    happy = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake"]
    )
    assert happy.returncode == 1  # honest blocked without a QC policy

    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable_after_fixes",
         "--comments", "全体として良好",
         "--dimension-comments", "audio_finishing=BGMをもう少し下げたい",
         "--dimension-comments", "subtitle=固有名詞は正確"]
    )
    assert result.returncode == 0, result.stderr
    review = PublishabilityReviewV1.model_validate_json(
        (episode_root / "review" / "publishability.json").read_bytes()
    )
    assert review.publishable == "after_small_corrections"
    assert review.episode_id == EPISODE_ID
    assert review.run_id.endswith("-finishing")
    assert review.overall_comment is not None
    assert "全体として良好" in review.overall_comment
    assert "audio_finishing: BGMをもう少し下げたい" in review.overall_comment
    assert "subtitle: 固有名詞は正確" in review.overall_comment


def test_record_publishability_rejects_unknown_dimension(episode_root: Path) -> None:
    _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable",
         "--dimension-comments", "not_a_domain=text"]
    )
    assert result.returncode == 2
    assert "dimension-unknown" in result.stderr


def test_record_time_appends_bootstrap_lines(tmp_path: Path) -> None:
    episode_root = tmp_path / "ep-time"
    episode_root.mkdir()
    for phase, minutes in (("ordinary_review", "12.5"), ("direct_resolve", "40")):
        result = _run_cli(
            ["record-time", "--episode-root", str(episode_root),
             "--phase", phase, "--minutes", minutes]
        )
        assert result.returncode == 0, result.stderr
    lines = [
        TimeLogLineV1.model_validate_json(line)
        for line in (episode_root / "time-log.jsonl").read_bytes().splitlines()
        if line.strip()
    ]
    assert [line.phase for line in lines] == ["ordinary_review", "direct_resolve"]
    assert [line.minutes for line in lines] == [12.5, 40.0]
    assert all(line.label == "bootstrap" for line in lines)
    assert all(line.schema_version == "v44-time-log-v1" for line in lines)


def test_record_time_rejects_non_positive_minutes(tmp_path: Path) -> None:
    episode_root = tmp_path / "ep-time-bad"
    episode_root.mkdir()
    result = _run_cli(
        ["record-time", "--episode-root", str(episode_root),
         "--phase", "troubleshooting", "--minutes", "0"]
    )
    assert result.returncode == 2  # argparse type refusal (malformed)


def test_review_store_resolution_prefers_the_live_cockpit_store(
    fixture_clip: Path, tmp_path: Path
) -> None:
    """Stale-state safety: the cockpit store wins over the frozen genesis."""

    record = _kit_record(*FULL_SELECTIONS)
    episode_root = _workspace(tmp_path, fixture_clip, record)
    log, store = resolve_review_store(episode_root)
    assert log == episode_root / "review" / "events.jsonl"
    assert store == episode_root / "review" / "store"

    # A later committed text in the live store is what finishing consumes.
    run_dir = episode_root / "run"
    shutil.rmtree(run_dir / "review-store")
    later = _seed_plan(text_1="後から適用された修正文です")
    init_review_store(later, run_dir / "review-store")
    log2, store2 = resolve_review_store(episode_root)
    assert (log2, store2) == (log, store)  # cockpit layout still preferred

    shutil.rmtree(episode_root / "review")
    log3, store3 = resolve_review_store(episode_root)
    assert log3 == run_dir / "review-store" / "events.jsonl"
    assert store3 == run_dir / "review-store"

    shutil.rmtree(run_dir / "review-store")
    with pytest.raises(Exception, match="review-store-missing"):
        resolve_review_store(episode_root)
