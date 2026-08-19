"""Phase-2 gate offline units (Todo 54): fault routing on the REAL stack.

Every named fault routes typed through the real compiler / render monitor /
privacy gate, and the gate evaluator recomputes all five frozen criteria
from synthesized raw evidence — detecting each injected defect and passing
the clean baseline. No Resolve, no ffmpeg renders here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.build.builder_models import BuildInterrupted, RenderTiming
from services.build.render_jobs import DEFAULT_RENDER_ATTEMPTS, RenderJobRunner
from services.build.render_models import OutputAllowlist, RenderJobFailure, RenderJobRequest
from services.fixtures.manifest_phase2 import FalseRenderCompleteFault, Phase2FixtureManifest
from services.job_runner.gate_p2_fake_tree import FaultKnobs
from services.job_runner.gate_p2_faults import FAULTS, evaluate_fake
from services.job_runner.gate_p2_finalize import fixture_privacy_declarations
from services.job_runner.gate_p2_ir import (
    compile_package,
    production_ir,
    stale_lock_for,
    wrong_media_presented,
)
from services.job_runner.gate_p2_live import CountingPool
from services.job_runner.gate_phase2 import load_policy
from services.qc.privacy_gate import evaluate_privacy_gate
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_bridge.fixed_presentation_fakes import FakeFpMediaPool
from services.toolchain.render_qc import render_complete
from tests.resolve_adapter.support import phase2_lock

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-2")


def manifest(fixture_id: str) -> Phase2FixtureManifest:
    return Phase2FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )



def test_stale_capability_fault_is_a_typed_compile_refusal() -> None:
    fault_manifest = manifest("p2-stale-capability")
    assert fault_manifest.fault.kind == "stale-capability"
    stale = stale_lock_for(fault_manifest)
    assert stale.resolve_package.capability_matrix_sha256 == (
        fault_manifest.fault.declared_matrix_sha256
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_package(fault_manifest, lock_override=stale)
    assert raised.value.code == "capability-matrix-stale"


def test_stale_lock_differs_from_the_pinned_matrix() -> None:
    stale = stale_lock_for(manifest("p2-stale-capability"))
    assert stale.resolve_package.capability_matrix_sha256 != (
        phase2_lock().resolve_package.capability_matrix_sha256
    )


def test_wrong_media_fault_is_a_typed_compile_refusal() -> None:
    fault_manifest = manifest("p2-same-duration-wrong-media")
    assert fault_manifest.fault.kind == "same-duration-wrong-media"
    presented = wrong_media_presented(fault_manifest)
    assert presented[0].sha256 == fault_manifest.fault.presented_media_sha256
    assert presented[0].duration_frames == fault_manifest.fault.presented_duration_frames
    with pytest.raises(PackageCompileError) as raised:
        compile_package(fault_manifest, presented_media=presented)
    assert raised.value.code == "media-hash-drift"


def test_clean_compile_succeeds_for_every_fixture() -> None:
    for fixture_id in (
        "p2-stale-capability",
        "p2-partial-build-restart",
        "p2-same-duration-wrong-media",
        "p2-false-render-complete",
        "p2-blocking-qc-privacy",
    ):
        package = compile_package(manifest(fixture_id))
        assert package.placements, fixture_id
        assert package.content_hash != "0" * 64


def test_production_ir_carries_the_manifest_base_records() -> None:
    ir = production_ir(manifest("p2-blocking-qc-privacy"))
    video = [item for track in ir.tracks if track.track.kind == "video" for item in track.items]
    subtitle = [
        item
        for track in ir.tracks
        if track.track.kind == "subtitle"
        for item in track.items
        if hasattr(item, "text")
    ]
    assert len(video) == 4
    assert len(subtitle) == 4


class LyingRenderProject:
    """The false-complete seam: a job claiming localized completion at 99%."""

    def __init__(self, status: str, percentage: int) -> None:
        self._status = status
        self._percentage = percentage
        self.render_dir: Path | None = None

    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool:  # noqa: N802
        return True

    def SetRenderSettings(self, settings: dict[str, object]) -> bool:  # noqa: N802
        self.render_dir = Path(str(settings["TargetDir"]))
        return True

    def AddRenderJob(self) -> str:  # noqa: N802
        return "job-lying-1"

    def StartRendering(self, job_id: str) -> bool:  # noqa: N802
        return True

    def StopRendering(self) -> None:  # noqa: N802
        return None

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]:  # noqa: N802
        return {"JobStatus": self._status, "CompletionPercentage": self._percentage}

    def GetRenderJobList(self) -> list[dict[str, object]]:  # noqa: N802
        return [
            {
                "JobId": "job-lying-1",
                "TargetDir": str(self.render_dir),
                "OutputFilename": "lying.mp4",
            }
        ]


def test_false_complete_status_never_completes_the_frozen_rule() -> None:
    fault = manifest("p2-false-render-complete").fault
    assert isinstance(fault, FalseRenderCompleteFault)
    lying = {"JobStatus": fault.reported_job_status, "CompletionPercentage": 99}
    assert render_complete(lying) is False
    assert render_complete({"JobStatus": fault.reported_job_status, "CompletionPercentage": 100})


def test_false_complete_trap_fails_permanently_and_bounded(tmp_path: Path) -> None:
    fault = manifest("p2-false-render-complete").fault
    assert isinstance(fault, FalseRenderCompleteFault)
    project = LyingRenderProject(fault.reported_job_status, 99)
    runner = RenderJobRunner(
        project,
        OutputAllowlist((tmp_path,)),
        timing=RenderTiming(deadline_seconds=5.0, poll_seconds=0.01),
    )
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(
            RenderJobRequest(
                preset=compile_package(manifest("p2-false-render-complete")).render_job,
                render_dir=str(tmp_path),
                custom_name="lying",
                timeline_conformance_fingerprint="0" * 64,
            )
        )
    assert raised.value.code == "render-false-complete"
    assert raised.value.failure_class == "permanent"
    assert raised.value.attempts <= DEFAULT_RENDER_ATTEMPTS


def test_privacy_declaration_blocks_publish_behind_a_human_gate() -> None:
    fault = manifest("p2-blocking-qc-privacy").fault
    assert fault.kind == "blocking-qc-privacy"
    declarations = fixture_privacy_declarations(manifest("p2-blocking-qc-privacy"))
    issues, gates = evaluate_privacy_gate(declarations, "qc-thresholds-test", ("a" * 64,))
    assert len(gates) == 1
    assert gates[0].rule_id == "privacy_rights_unresolved"
    assert all(issue.severity == "blocker" for issue in issues)


def test_interrupt_after_partial_placements_raises_the_kill_seam() -> None:
    fault_manifest = manifest("p2-partial-build-restart")
    fault = fault_manifest.fault
    assert fault.kind == "partial-build-restart"
    pool = FakeFpMediaPool("")
    pool.CreateEmptyTimeline("__fvp_test__timeline_x")
    pool_item = pool.ImportMedia(["p2-gate-unit-media.mov"])[0]
    counting = CountingPool(pool, interrupt_after_placed_items=3)
    package = compile_package(fault_manifest)

    def place_all() -> None:
        for group in (package.placements[:2], package.placements[2:4], package.placements[4:]):
            infos = [
                {
                    "mediaPoolItem": pool_item,
                    "startFrame": placement.clip_info.start_frame,
                    "endFrame": placement.clip_info.end_frame,
                    "mediaType": 1,
                    "trackIndex": 1,
                    "recordFrame": placement.clip_info.record_frame,
                }
                for placement in group
            ]
            counting.AppendToTimeline(infos)

    with pytest.raises(BuildInterrupted):
        place_all()
    assert fault.interrupt_after_placed_items <= counting.placed_items < len(package.placements)


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fault_is_detected_from_recomputed_evidence(fault: str, tmp_path: Path) -> None:
    policy, policy_sha256 = load_policy(Path("config/gates/phase-2-v1.json"))
    baseline = evaluate_fake(policy, policy_sha256, tmp_path / "baseline", FaultKnobs())
    probe = evaluate_fake(policy, policy_sha256, tmp_path / "fault", FaultKnobs(fault=fault))
    assert baseline.result.passed, (fault, baseline.mismatches)
    assert not probe.result.passed, (fault, probe.mismatches)
    assert probe.expected_code in {code for code, _detail in probe.mismatches}, (
        fault,
        probe.mismatches,
    )


def test_fake_baseline_passes_every_criterion(tmp_path: Path) -> None:
    policy, policy_sha256 = load_policy(Path("config/gates/phase-2-v1.json"))
    outcome = evaluate_fake(policy, policy_sha256, tmp_path, FaultKnobs())
    assert outcome.result.passed, list(outcome.mismatches)
    assert outcome.marker is not None
    assert outcome.marker.fixture_records_are_publication_decisions is False
    assert all(binding.fixture_record_fixture_only for binding in outcome.marker.fixtures)
