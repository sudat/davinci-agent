"""Task 13: live finishing + QC + publishability protocol (Tier B, offline).

The pinned-ffmpeg fixture renders are REAL media; the MCP executor is the
fake replay (readback-verified, no server) — the mcp_execution fake-executor
test precedent. Every blocked path asserts a typed non-zero refusal, never
a misleading success. The live-executor refusal is proven at the seam
(episode0's bounded probe raises before any execution).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest

from services.cli import _v44_finishing_exec as exec_mod
from services.cli import _v44_finishing_run, episode0, v44_finishing
from services.cli._v44_finishing_build import FinishingError, resolve_review_store
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
from services.creative_plan.audio_finishing import AudioFactsV1
from services.final_review.publishability import PublishabilityReviewV1
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.mcp_client.call_models import McpExecutionReportV1
from services.mcp_execution.live_adapter import LiveMcpAdapter
from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.live_handlers.placement import (
    PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
)
from services.mcp_execution.plan_models import McpExecutionPlanV1
from services.mcp_execution.runner import McpExecutionRunReportV1, StepAttemptV1, StepResultV1
from services.metrics.v44_gate_state import V44GateSummaryV1
from services.preview.errors import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools
from services.production_kit.preview import KitDomainSelectionV1, KitSelectionRecordV1
from services.qc.policy_build import build_policy

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from services.mcp_client.execution_runner import McpTransportFn

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
    # Task 5 moved the dialogue-chain audio stages onto the granular MCP
    # rung (no fallback record); this fixture enables no matrix-failed
    # capability, so the plan carries zero fallback rungs.
    assert report["fallback_rung_count"] == 0
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


def test_run_fake_consumes_episode_audio_facts(episode_root: Path) -> None:
    """The production run route honors an episode-scoped AudioFactsV1 file.

    Defect lock (focused rerun8 evidence on v44-real-01): without injection
    the conservative default forces dialogue_cleanup on clean dialogue media.
    """

    episode_root.joinpath("finishing").mkdir(parents=True, exist_ok=True)
    facts_path = episode_root / "finishing" / "audio-facts.json"
    facts_path.write_bytes(
        AudioFactsV1(
            episode_id=EPISODE_ID,
            dialogue_clean=True,
            has_bgm=False,
            has_ambience=False,
            measured_loudness_ok=True,
        ).model_dump_json().encode()
    )

    result = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake",
         "--audio-facts", str(facts_path)]
    )
    assert result.returncode == 1, result.stderr  # still no QC policy — honest blocked

    plan = json.loads((episode_root / "finishing" / "audio-plan.json").read_bytes())
    stages = {row["stage"]: row for row in plan["stages"]}
    assert stages["dialogue_cleanup"]["enabled"] is False
    assert "already-good" in (stages["dialogue_cleanup"]["justification"] or "")
    assert stages["dialogue_level_normalization"]["enabled"] is False
    assert "already-good" in (stages["dialogue_level_normalization"]["justification"] or "")


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


def test_resolve_executor_live_wraps_with_media_mapping_and_fake_unaffected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_probe(pin_path: Path) -> McpTransportFn:
        def raw(
            tool_name: str,
            action: str,
            normalized_params: Mapping[str, object],
            *,
            timeout_seconds: float | None = None,
        ) -> object:
            return {"success": True}

        return raw

    monkeypatch.setattr(_v44_finishing_run, "_probe_live_executor", fake_probe)

    monkeypatch.setattr(exec_mod, "_media_frame_counts", lambda paths: {})

    args_live = argparse.Namespace(executor="live", pin=Path("pin.json"))
    dummy_plan = McpExecutionPlanV1.model_construct()
    media_paths = {
        "ep-457dfac97989568e-edit-source": str(tmp_path / "edit-source.mov")
    }
    executor, _note = _v44_finishing_run.resolve_executor(
        args_live, dummy_plan, media_paths
    )
    assert isinstance(executor, LiveMcpAdapter)
    # fake must stay unwrapped even when mapping is supplied
    sentinel = object()
    monkeypatch.setattr(_v44_finishing_run, "FakePlanExecutor", lambda plan: sentinel)
    args_fake = argparse.Namespace(executor="fake", pin=Path("pin.json"))
    executor2, note2 = _v44_finishing_run.resolve_executor(
        args_fake, dummy_plan, media_paths
    )
    assert executor2 is sentinel
    assert "fake" in note2


def test_probe_live_executor_forwards_operation_deadline_to_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product live executor (the finishing route's raw transport) must
    forward an operation-specific ``timeout_seconds`` into the JSON-RPC
    request — the measured placement scan needs 324.41s and the 30s config
    default killed it (repair-5 verification failure)."""
    requests: list[tuple[str, float | None]] = []

    class _StubTransport:
        def request(
            self,
            method: str,
            params: object,
            *,
            timeout_seconds: float | None = None,
        ) -> dict[str, object]:
            requests.append((method, timeout_seconds))
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"success": true, "occurrences": []}',
                        }
                    ],
                    "isError": False,
                }
            }

    class _StubClient:
        transport = _StubTransport()

        def connect(self) -> object:
            return None

        def resolve_get_version(self) -> object:
            return None

    monkeypatch.setattr(episode0, "_live_client_from_pin", lambda _pin: _StubClient())

    executor = episode0._probe_live_executor(Path("pin.json"))

    executor(
        "timeline",
        "source_range_report",
        {},
        timeout_seconds=PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
    )
    executor("timeline", "get_current", {})

    assert requests[0] == ("tools/call", PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS)
    assert requests[1] == ("tools/call", None)


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


# ---------------------------------------------------------------------------
# gate-summary (T16): derives v44-gate-summary-v1; refusals are the
# anti-fabrication guarantee.
# ---------------------------------------------------------------------------


def test_gate_summary_refuses_without_finishing_report(tmp_path: Path) -> None:
    episode_root = tmp_path / "ep-no-finishing"
    episode_root.mkdir()
    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "finishing-report-missing" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_refuses_without_publishability_record(
    episode_root: Path,
) -> None:
    """The plan's failure drill: drop the publishability record -> typed refusal."""

    run = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert run.returncode == 1  # honest blocked without a QC policy
    assert (episode_root / "finishing" / "finishing-run.json").is_file()

    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "publishability-missing" in result.stderr
    assert "record-publishability" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_honest_fail_then_pass_after_full_evidence(
    episode_root: Path,
) -> None:
    """No time log yet -> passed=false summary (written, exit 1); after the
    operator records time + a passing rerun + verdict -> passed=true."""

    run = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert run.returncode == 1  # no QC policy yet
    verdict = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable"]
    )
    assert verdict.returncode == 0, verdict.stderr

    early = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert early.returncode == 1  # blocked domain + null QC + no time log
    summary_path = episode_root / "finishing" / "gate-summary.json"
    early_summary = json.loads(summary_path.read_bytes())
    assert early_summary["passed"] is False
    assert early_summary["bootstrap_aht_minutes"] is None
    assert early_summary["direct_resolve_minutes"] is None
    assert "delivery_qc" in early_summary["blocked_domains"]
    assert "not passed:" in early.stderr

    # T8: AHT exists only when ALL five canonical bootstrap phases are
    # recorded — a partial log keeps bootstrap_aht_minutes null (blocked).
    for phase, minutes in (
        ("ordinary_review", "12.5"),
        ("kit_bootstrap", "3.5"),
        ("taste_calibration", "1.5"),
        ("troubleshooting", "2.5"),
        ("direct_resolve", "40"),
    ):
        timed = _run_cli(
            ["record-time", "--episode-root", str(episode_root),
             "--phase", phase, "--minutes", minutes]
        )
        assert timed.returncode == 0, timed.stderr

    policy_path = episode_root / "finishing" / "qc-policy.json"
    policy = build_policy(episode_root / "finishing" / "final-preview" / "preview.mp4")
    atomic_write(policy_path, canonical_model_bytes(policy))
    passing = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake",
         "--qc-policy", str(policy_path)]
    )
    assert passing.returncode == 0, passing.stderr

    final = _run_cli(
        ["gate-summary", "--episode-root", str(episode_root),
         "--subtitle-proof-ref", "finishing/subtitle-proof/subtitle-proof.json"]
    )
    assert final.returncode == 0, final.stderr
    summary = V44GateSummaryV1.model_validate_json(summary_path.read_bytes())
    assert summary.passed is True
    assert summary.operator_verdict == "publishable"
    assert summary.blocked_domains == ()
    assert summary.technical_qc == "passed"
    assert summary.editorial_qc_blocked_items == 0
    assert summary.bootstrap_aht_minutes == 60.0
    assert summary.direct_resolve_minutes == 40.0
    assert summary.director_pin_model == "gpt-5.6-sol"
    assert summary.evidence_pins["analysis_provider"].startswith("whisper-cpp-cli:")
    assert summary.subtitle_proof_ref == "finishing/subtitle-proof/subtitle-proof.json"
    assert "finishing/finishing-run.json" in summary.artifacts
    assert "review/publishability.json" in summary.artifacts
    assert "time-log.jsonl" in summary.artifacts


def test_gate_summary_not_publishable_verdict_never_passes(
    episode_root: Path,
) -> None:
    run = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert run.returncode == 1
    verdict = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "not_publishable"]
    )
    assert verdict.returncode == 0, verdict.stderr
    for phase, minutes in (("ordinary_review", "5"), ("direct_resolve", "1.5")):
        _run_cli(
            ["record-time", "--episode-root", str(episode_root),
             "--phase", phase, "--minutes", minutes]
        )

    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "operator verdict is not_publishable" in result.stderr
    summary = V44GateSummaryV1.model_validate_json(
        (episode_root / "finishing" / "gate-summary.json").read_bytes()
    )
    assert summary.passed is False
    assert summary.operator_verdict == "not_publishable"
    # Partial log (two of five phases): AHT stays null, never a partial sum.
    assert summary.bootstrap_aht_minutes is None


def test_mcp_failed_outcome_blocks_before_preview(
    fixture_clip: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failed MCP execution must not render a misleading local preview (D4).

    Given: a finishing run whose MCP execution report outcome is failed
    (e.g. prepare_project Unknown tool)
    When: cmd_run is invoked
    Then: it writes mcp-run-report.json, exits 1 with BLOCKED and the
          failed-step summary, and never calls render_review_preview.
    """

    episode_root = _workspace(tmp_path, fixture_clip, _kit_record(*FULL_SELECTIONS))

    failed_step = StepResultV1(
        step_id="step-prepare_project-001",
        action="prepare_project",
        rung="mcp_verified_workflow",
        status="failed",
        attempts=(StepAttemptV1(attempt=1, status="error", detail="Unknown tool"),),
        failure_code="executor-error",
        detail="Unknown tool: prepare_project",
    )

    def fake_execute_plan(*args: object, **kwargs: object) -> McpExecutionRunReportV1:
        return McpExecutionRunReportV1(
            schema_version="mcp-execution-run-report-v1",
            plan_id="a" * 64,
            episode_id=EPISODE_ID,
            outcome="failed",
            steps=(failed_step,),
            rung_entries=(),
            calls=McpExecutionReportV1(
                total_calls=1,
                by_status={"error": 1},
                by_tool={"prepare_project": 1},
                window_started_at=1000,
                window_finished_at=1001,
            ),
            started_at=1000,
            finished_at=1001,
        )

    monkeypatch.setattr(_v44_finishing_run, "execute_plan", fake_execute_plan)

    calls: list[int] = []

    def fake_render(*args: object, **kwargs: object) -> Path:
        calls.append(1)
        return episode_root / "finishing" / "final-preview" / "preview.mp4"

    monkeypatch.setattr(_v44_finishing_run, "render_review_preview", fake_render)

    code = v44_finishing.main(
        ["run", "--episode-root", str(episode_root), "--executor", "fake"]
    )
    captured = capsys.readouterr()

    assert code == v44_finishing.EXIT_BLOCKED
    assert "mcp-execution-failed" in captured.err
    assert "1" in captured.err  # failed step count
    assert "prepare_project" in captured.err  # first step
    assert "executor-error" in captured.err  # failure code
    assert "BLOCKED" in captured.err
    assert not calls, "render_review_preview was called despite failed MCP outcome"
    assert (episode_root / "finishing" / "mcp-run-report.json").is_file()
    report = json.loads((episode_root / "finishing" / "mcp-run-report.json").read_bytes())
    assert report["outcome"] == "failed"
    assert not (episode_root / "finishing" / "final-preview" / "preview.mp4").is_file()
    assert not (episode_root / "finishing" / "finishing-run.json").is_file()


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 1 — native-finishing gate regressions.
# ---------------------------------------------------------------------------


def _run_to_passing_fake_report(episode_root: Path) -> dict[str, object]:
    """Two-step honest flow: the first run renders the preview (blocked on
    missing QC policy), then a policy-backed rerun passes the gate."""

    first = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert first.returncode == 1, first.stderr
    policy = build_policy(episode_root / "finishing" / "final-preview" / "preview.mp4")
    policy_path = episode_root / "finishing" / "qc-policy.json"
    atomic_write(policy_path, canonical_model_bytes(policy))
    passing = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake",
         "--qc-policy", str(policy_path)]
    )
    assert passing.returncode == 0, passing.stderr
    report = json.loads((episode_root / "finishing" / "finishing-run.json").read_bytes())
    assert report["gate_decision"] == "pass"
    return report


def test_fake_executor_success_never_counts_as_native_finishing(
    episode_root: Path,
) -> None:
    """Gate regression (fake executor): a completed fake run self-identifies —
    executor=fake with a note disclaiming the MCP server and Resolve — and
    fabricates no native render proof, so it can never satisfy native
    finishing success (misleading-success guard)."""

    report = _run_to_passing_fake_report(episode_root)

    assert report["executor"] == "fake"
    note = report["executor_note"]
    assert isinstance(note, str)
    assert "no MCP server" in note
    assert "no Resolve" in note
    assert report.get("native_render") is None
    # The fake run's final preview is the local pinned-tool render, not a
    # DaVinci render output — that is exactly why it is not native proof.
    preview_path = report["final_preview_path"]
    assert isinstance(preview_path, str)
    assert preview_path.endswith("preview.mp4")


def _write_native_meta(
    target: Path, *, job_id: str, **overrides: object
) -> Path:
    base: dict[str, object] = {
        "schema_version": "native-render-meta-v1",
        "custom_name": "finishing-native-ep-test",
        "job_id": job_id,
        "output_path": str(target),
        "output_sha256": sha256_file(target),
        "duration_seconds": 1.0,
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "30/1",
        "audio_codec": "aac",
        "audio_channels": 2,
        "audio_sample_rate": 48000,
        "has_subtitle_stream": False,
    }
    base.update(overrides)
    meta_path = target.parent / "finishing-native-ep-test.meta.json"
    meta_path.write_text(json.dumps(base))
    return meta_path


def test_load_native_render_builds_block_from_measured_fields(
    tmp_path: Path,
) -> None:
    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    subprocess.run(
        (
            "ffmpeg",
            "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-shortest", str(out),
        ),
        check=False, capture_output=True, timeout=30,
    )
    assert out.is_file()
    _write_native_meta(out, job_id="job-measured-1")
    loaded = _load_native_render(tmp_path, "ep-test")
    assert loaded is not None
    _preview_path, sha, block = loaded
    assert block.job_id == "job-measured-1"
    assert block.output_sha256 == sha
    assert sha == sha256_file(out)
    assert block.duration_seconds > 0
    assert block.video_codec == "h264"
    assert block.width == 1920
    assert block.height == 1080
    assert block.audio_codec == "aac"
    assert block.audio_channels == 2


def test_load_native_render_refuses_missing_measured_fields(
    tmp_path: Path,
) -> None:
    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x00" * 64)
    meta = _write_native_meta(out, job_id="job-x")
    raw = json.loads(meta.read_text())
    del raw["duration_seconds"]
    meta.write_text(json.dumps(raw))
    assert _load_native_render(tmp_path, "ep-test") is None


def test_load_native_render_refuses_sentinel_job_id(tmp_path: Path) -> None:
    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x00" * 64)
    _write_native_meta(out, job_id="reused-existing")
    assert _load_native_render(tmp_path, "ep-test") is None


def test_load_native_render_refuses_wrong_custom_name(tmp_path: Path) -> None:
    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x00" * 64)
    _write_native_meta(out, job_id="job-x", custom_name="someone-elses-name")
    assert _load_native_render(tmp_path, "ep-test") is None


def test_load_native_render_refuses_wrong_output_path(tmp_path: Path) -> None:
    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x00" * 64)
    _write_native_meta(out, job_id="job-x", output_path="/elsewhere/renamed.mp4")
    assert _load_native_render(tmp_path, "ep-test") is None


def test_load_native_render_refuses_tampered_media_facts(
    tmp_path: Path,
) -> None:
    """Editable metadata cannot inject report values: every recorded fact
    must equal an independent ffprobe re-measurement."""

    from services.cli._v44_finishing_run import (  # noqa: PLC0415
        _load_native_render,
    )

    out = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True)
    subprocess.run(
        (
            "ffmpeg",
            "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-shortest", str(out),
        ),
        check=False, capture_output=True, timeout=30,
    )
    assert out.is_file()
    _write_native_meta(out, job_id="job-x", duration_seconds=99.0, width=3840)
    assert _load_native_render(tmp_path, "ep-test") is None


def test_future_finishing_binds_final_preview_to_resolve_native_render(
    tmp_path: Path,
) -> None:
    """Native render block binds the real media's own identity (unit proof)."""

    import subprocess  # noqa: PLC0415

    repo = Path(__file__).resolve().parents[2]
    ffmpeg = repo / ".venv" / "bin" / "ffmpeg"
    ffmpeg_bin = str(ffmpeg) if ffmpeg.is_file() else "ffmpeg"
    target = tmp_path / "final-resolve-render" / "finishing-native-ep-test.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        (
            ffmpeg_bin,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(target),
        ),
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert target.is_file()
    assert target.stat().st_size > 0
    from services.cli._v44_finishing_report import NativeRenderBlockV1  # noqa: PLC0415

    native = NativeRenderBlockV1(
        job_id="job-native-unit-1",
        output_path=str(target),
        output_sha256=sha256_file(target),
        output_size_bytes=target.stat().st_size,
        duration_seconds=1.0,
        video_codec="h264",
        width=320,
        height=180,
        audio_codec="aac",
        audio_channels=2,
    )
    assert sha256_file(Path(native.output_path)) == native.output_sha256
    assert native.output_size_bytes > 0


def test_resolve_executor_probes_media_eof_into_the_live_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live arm must establish trusted per-source media frame EOFs
    (ffprobe nb_frames) and hand them to the adapter — the EOF-aware
    placement reconciliation is inactive without them (r3/r4 blocker)."""

    media = tmp_path / "edit-source.mov"
    media.write_bytes(b"stub")

    def fake_probe(pin: Path) -> object:
        def raw(*args: object, **kwargs: object) -> object:
            return {"success": True}

        return raw

    monkeypatch.setattr(_v44_finishing_run, "_probe_live_executor", fake_probe)

    def fake_counts(paths: object) -> dict[str, int]:
        return {"ep-457dfac97989568e-edit-source": 8467}

    monkeypatch.setattr(exec_mod, "_media_frame_counts", fake_counts)

    args_live = argparse.Namespace(executor="live", pin=Path("pin.json"))
    dummy_plan = McpExecutionPlanV1.model_construct()
    executor, _note = _v44_finishing_run.resolve_executor(
        args_live, dummy_plan, {"ep-457dfac97989568e-edit-source": str(media)}
    )
    assert isinstance(executor, LiveMcpAdapter)
    assert executor._ctx.media_frame_counts == {  # wiring proof
        "ep-457dfac97989568e-edit-source": 8467
    }


def test_resolve_executor_blocks_typed_when_media_eof_unprobeable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "edit-source.mov"
    media.write_bytes(b"stub")

    def fake_probe(pin: Path) -> object:
        def raw(*args: object, **kwargs: object) -> object:
            return {"success": True}

        return raw

    monkeypatch.setattr(_v44_finishing_run, "_probe_live_executor", fake_probe)

    def broken_counts(paths: object) -> dict[str, int]:
        raise LiveAdapterError("media-frame-count-missing", "no nb_frames")

    monkeypatch.setattr(exec_mod, "_media_frame_counts", broken_counts)

    args_live = argparse.Namespace(executor="live", pin=Path("pin.json"))
    dummy_plan = McpExecutionPlanV1.model_construct()

    with pytest.raises(FinishingError) as exc:
        _v44_finishing_run.resolve_executor(
            args_live, dummy_plan, {"src": str(media)}
        )
    assert exc.value.code == "media-frame-count-unavailable"
