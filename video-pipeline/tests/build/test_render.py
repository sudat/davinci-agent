"""Todo-51 acceptance: deterministic Final Render lifecycle + independent validation.

Offline (fake Deliver page + fake inspection tools): the REAL runner and
validator code exercises every lifecycle fault — false-complete localized
status, bounded timeout retry, connection drop, explicit cancel,
non-allowlisted target, missing output — and every validation fault — wrong
preset binding, wrong timeline fingerprint, truncated/undecodable bytes,
metadata mismatch — each surfacing as a typed failure with no silent pass.
A passing record structurally requires the raw ffprobe JSON and a clean
decode log, so API success alone can never validate an output.

Live (``-m resolve_live``): a real staging timeline is built and conformed,
rendered via the runner from the pinned preset, and the output is validated
and bound to that exact timeline's conformance fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import ValidationError

from services.build.builder_apply import (
    apply_link_groups,
    ensure_track_layout,
    import_media,
    place_items,
)
from services.build.builder_models import RenderTiming
from services.build.builder_recover import sweep_orphan_stagings, verify_declared_media
from services.build.builder_render import PinnedBuildTools
from services.build.builder_session import (
    create_staging,
    delete_staging,
    staging_project_name,
)
from services.build.conformance import verify_built_conformance
from services.build.render_jobs import OutputAllowlist, RenderCancel, RenderJobRunner
from services.build.render_models import (
    DecodeEvidence,
    RenderColorTable,
    RenderJobAttempt,
    RenderJobFailure,
    RenderJobRecord,
    RenderJobRequest,
    RenderMismatch,
    RenderOutputBinding,
    RenderPresetExpectation,
    ValidatedRenderOutput,
    preset_sha256,
)
from services.build.render_validate import (
    PinnedRenderInspectionTools,
    validate_render_output,
)
from services.contracts.primitives import RationalFrameRate
from services.resolve_adapter.models import RenderJobSpec
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection
from services.resolve_bridge.fixed_presentation_models import FfprobeStream
from services.resolve_bridge.fixed_presentation_tools import DecodeOutcome
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.lifecycle import project_names
from services.resolve_bridge.readiness import load_host_report
from tests.build.support import compile_build_package, manifest_0a
from tests.resolve_locale import native_bridge_locale
from tests.work_init_support import FROZEN_ATTEMPT_DIR

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.resolve_adapter.models import ResolvePackage

FAKE_RENDER_BYTES: Final = b"fake-render-output-0123456789abcdef"
FINGERPRINT: Final = "a" * 64
WRONG_FINGERPRINT: Final = "b" * 64


def offline_preset() -> RenderJobSpec:
    return RenderJobSpec(
        video_format="MP4",
        video_codec="H264",
        width=1920,
        height=1080,
        frame_rate=RationalFrameRate(num=30, den=1),
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=2,
        timeline_start_timecode="01:00:00:00",
        frame_origin=108000,
        extent_frames=660,
    )


def offline_expectation() -> RenderPresetExpectation:
    return RenderPresetExpectation(
        container_format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        width=1920,
        height=1080,
        r_frame_rate="30/1",
        pix_fmt="yuv420p",
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=2,
        expected_nb_frames="660",
    )


class FakeDeliverProject:
    """Deliver-page fake: the real runner drives it through the job lifecycle."""

    def __init__(
        self, render_dir: Path, fault: str = "", cancel: RenderCancel | None = None
    ) -> None:
        self._render_dir = render_dir
        self._fault = fault
        self._cancel = cancel
        self._settings: dict[str, object] = {}
        self.jobs: list[dict[str, object]] = []
        self.added = 0
        self.started: list[str] = []
        self.stopped = 0

    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool:  # noqa: N802 (Resolve API)
        self._settings["__codec"] = f"{video_format}/{codec}"
        return True

    def SetRenderSettings(self, settings: dict[str, object]) -> bool:  # noqa: N802 (Resolve API)
        self._settings.update(settings)
        return True

    def AddRenderJob(self) -> str:  # noqa: N802 (Resolve API)
        self.added += 1
        job_id = f"render-job-{self.added}"
        target = str(self._settings.get("TargetDir", str(self._render_dir)))
        custom = str(self._settings.get("CustomName", "render"))
        name = f"{custom}.mp4"
        output = Path(target) / name
        if self._fault not in ("missing_output", "rogue_target"):
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                FAKE_RENDER_BYTES[:7] if self._fault == "partial_output" else FAKE_RENDER_BYTES
            )
            output.write_bytes(payload)
        entry: dict[str, object] = {
            "JobId": job_id,
            "TargetDir": target,
            "OutputFilename": name,
            "MarkIn": 108000,
            "MarkOut": 108659,
        }
        if self._fault == "rogue_target":
            entry["TargetDir"] = str(self._render_dir.parent / "elsewhere")
        self.jobs.append(entry)
        return job_id

    def StartRendering(self, job_id: str) -> bool:  # noqa: N802 (Resolve API)
        self.started.append(job_id)
        return True

    def StopRendering(self) -> None:  # noqa: N802 (Resolve API)
        self.stopped += 1

    def GetRenderJobList(self) -> list[dict[str, object]]:  # noqa: N802 (Resolve API)
        return list(self.jobs)

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]:  # noqa: N802 (Resolve API)
        if self._fault == "false_complete":
            return {"JobStatus": "完了", "CompletionPercentage": 99}
        if self._fault == "complete_no_percentage":
            return {"JobStatus": "Complete"}
        if self._fault == "stalled":
            return {"JobStatus": "Rendering", "CompletionPercentage": 47}
        if self._fault == "disconnect":
            raise BridgeConnectionError("simulated bridge connection drop mid-render")
        if self._fault == "cancel" and self._cancel is not None:
            self._cancel.requested = True
            self._cancel.detail = "operator requested stop mid-render"
            return {"JobStatus": "Rendering", "CompletionPercentage": 47}
        return {"JobStatus": "完了", "CompletionPercentage": 100, "TimeTakenToRenderInMs": 12}


class FakeInspectionTools:
    """Canned raw ffprobe JSON plus a magic-bytes decode over the fake output."""

    def __init__(self, *, audio_codec: str = "aac", decode_fault: bool = False) -> None:
        self._audio_codec = audio_codec
        self._decode_fault = decode_fault

    def probe_raw(self, path: Path) -> str:
        video = {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "nb_frames": "660",
            "duration": "22.000000",
            "color_space": "bt709",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_range": "tv",
        }
        audio = {
            "codec_type": "audio",
            "codec_name": self._audio_codec,
            "sample_rate": "48000",
            "channels": 2,
            "duration": "22.080000",
        }
        payload = {
            "streams": [video, audio],
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "22.080000"},
        }
        return json.dumps(payload)

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def decode(self, path: Path) -> DecodeOutcome:
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        decodable = data == FAKE_RENDER_BYTES and not self._decode_fault
        argv = (
            f"ffmpeg({path.name})",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        )
        return DecodeOutcome(
            argv=argv,
            exit_code=0 if decodable else 1,
            stderr_tail="" if decodable else "simulated decode failure",
        )


def offline_rig(
    tmp_path: Path,
    *,
    fault: str = "",
    cancel: RenderCancel | None = None,
    timing: RenderTiming | None = None,
    max_attempts: int = 2,
) -> tuple[RenderJobRunner, RenderJobRequest, FakeDeliverProject]:
    render_dir = tmp_path / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    project = FakeDeliverProject(render_dir, fault=fault, cancel=cancel)
    runner = RenderJobRunner(
        project,
        OutputAllowlist((render_dir,)),
        timing=timing
        if timing is not None
        else RenderTiming(deadline_seconds=5.0, poll_seconds=0.01),
        max_attempts=max_attempts,
        cancel=cancel,
    )
    request = RenderJobRequest(
        preset=offline_preset(),
        render_dir=str(render_dir),
        custom_name="final",
        timeline_conformance_fingerprint=FINGERPRINT,
    )
    return runner, request, project


def offline_validate(
    record: RenderJobRecord,
    request: RenderJobRequest,
    tools: FakeInspectionTools | None = None,
    *,
    authoritative_fingerprint: str = FINGERPRINT,
    preset: RenderJobSpec | None = None,
) -> ValidatedRenderOutput:
    return validate_render_output(
        record,
        preset=preset if preset is not None else request.preset,
        expectation=offline_expectation(),
        authoritative_fingerprint=authoritative_fingerprint,
        tools=tools if tools is not None else FakeInspectionTools(),
    )


# ------------------------------------------------------------- offline: happy


def test_offline_happy_render_validates_and_binds(tmp_path: Path) -> None:
    runner, request, project = offline_rig(tmp_path)
    record = runner.run(request)
    assert record.job_id == "render-job-1"
    assert record.attempts[-1].completion_percentage == 100
    assert record.poll_count_total >= 1
    assert project.stopped == 0
    validated = offline_validate(record, request)
    assert validated.passed is True
    assert validated.binding.render_job_id == record.job_id
    assert validated.binding.timeline_conformance_fingerprint == FINGERPRINT
    assert validated.binding.preset_sha256 == preset_sha256(request.preset)
    assert validated.binding.output_sha256 == hashlib.sha256(FAKE_RENDER_BYTES).hexdigest()
    assert validated.output_size_bytes == len(FAKE_RENDER_BYTES)
    assert validated.video.width == 1920
    assert validated.video.r_frame_rate == "30/1"
    assert validated.audio.sample_rate == "48000"
    assert validated.color.color_space == "bt709"
    payload = json.loads(validated.ffprobe_json)
    assert payload["streams"][0]["codec_name"] == "h264"
    assert validated.decode.exit_code == 0
    assert "-f" in validated.decode.argv
    assert "null" in validated.decode.argv


# --------------------------------------------------- offline: lifecycle faults


def test_offline_false_complete_status_fails_permanently(tmp_path: Path) -> None:
    for fault in ("false_complete", "complete_no_percentage"):
        runner, request, project = offline_rig(tmp_path, fault=fault)
        with pytest.raises(RenderJobFailure) as raised:
            runner.run(request)
        failure = raised.value
        assert failure.code == "render-false-complete", fault
        assert failure.failure_class == "permanent", fault
        assert failure.attempts == 1, fault
        assert project.stopped == 1, fault
        assert project.added == 1, fault


def test_offline_missing_output_fails_typed(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path, fault="missing_output")
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    assert raised.value.code == "render-output-missing"
    assert raised.value.failure_class == "permanent"
    ghost = RenderJobRecord(
        preset_sha256=preset_sha256(request.preset),
        timeline_conformance_fingerprint=FINGERPRINT,
        output_path=str(tmp_path / "renders" / "ghost.mp4"),
        job_id="render-job-9",
        attempts=(
            RenderJobAttempt(
                attempt=1,
                job_id="render-job-9",
                outcome="complete",
                completion_percentage=100,
                poll_count=1,
            ),
        ),
        poll_count_total=1,
    )
    with pytest.raises(RenderJobFailure) as validation:
        offline_validate(ghost, request)
    assert validation.value.code == "output-missing"


def test_offline_partial_truncated_output_fails_decode(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path, fault="partial_output")
    record = runner.run(request)
    assert record.output_path
    with pytest.raises(RenderJobFailure) as raised:
        offline_validate(record, request)
    failure = raised.value
    assert failure.code == "decode-failed"
    assert failure.failure_class == "permanent"
    assert "decode" in failure.detail.lower() or failure.detail


def test_offline_timeout_retries_bounded_then_fails_typed(tmp_path: Path) -> None:
    timing = RenderTiming(deadline_seconds=0.2, poll_seconds=0.01)
    runner, request, project = offline_rig(
        tmp_path, fault="stalled", timing=timing, max_attempts=2
    )
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    failure = raised.value
    assert failure.code == "timeout"
    assert failure.failure_class == "transient"
    assert failure.attempts == 2
    assert project.added == 2
    assert project.stopped == 2
    assert len(failure.attempt_rows) == 2
    assert all(row.outcome == "timeout" for row in failure.attempt_rows)


def test_offline_connection_drop_is_transient_bounded(tmp_path: Path) -> None:
    runner, request, project = offline_rig(tmp_path, fault="disconnect", max_attempts=2)
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    failure = raised.value
    assert failure.code == "resolve_disconnect"
    assert failure.failure_class == "transient"
    assert failure.attempts == 2
    assert project.added == 2


def test_offline_explicit_cancel_fails_typed_without_retry(tmp_path: Path) -> None:
    cancel = RenderCancel()
    runner, request, project = offline_rig(tmp_path, fault="stalled", cancel=cancel,
                                           max_attempts=3)
    runner.cancel("operator stop requested")
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    failure = raised.value
    assert failure.code == "render-cancelled"
    assert failure.failure_class == "permanent"
    assert failure.attempts == 1
    assert "operator stop requested" in failure.detail
    assert project.added == 1
    assert project.stopped >= 1


def test_offline_cancel_mid_render_stops_the_job(tmp_path: Path) -> None:
    cancel = RenderCancel()
    runner, request, project = offline_rig(tmp_path, fault="cancel", cancel=cancel,
                                           max_attempts=3)
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    failure = raised.value
    assert failure.code == "render-cancelled"
    assert failure.failure_class == "permanent"
    assert "mid-render" in failure.detail
    assert project.stopped >= 1


def test_offline_non_allowlisted_path_refused(tmp_path: Path) -> None:
    runner, request, project = offline_rig(tmp_path, fault="rogue_target")
    with pytest.raises(RenderJobFailure) as raised:
        runner.run(request)
    failure = raised.value
    assert failure.code == "output-path-not-allowlisted"
    assert failure.failure_class == "permanent"
    assert project.started == [], "a non-allowlisted target must never start rendering"
    assert project.added == 1


# ------------------------------------------------ offline: validation faults


def test_offline_wrong_preset_binding_fails_typed(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path)
    record = runner.run(request)
    wrong = request.preset.model_copy(update={"audio_sample_rate": 44100})
    with pytest.raises(RenderJobFailure) as raised:
        offline_validate(record, request, preset=wrong)
    failure = raised.value
    assert failure.code == "preset-mismatch"
    assert failure.failure_class == "permanent"


def test_offline_wrong_fingerprint_fails_typed(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path)
    record = runner.run(request)
    with pytest.raises(RenderJobFailure) as raised:
        offline_validate(record, request, authoritative_fingerprint=WRONG_FINGERPRINT)
    failure = raised.value
    assert failure.code == "fingerprint-mismatch"
    assert failure.failure_class == "permanent"


def test_offline_decode_corrupt_output_fails_typed(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path)
    record = runner.run(request)
    with pytest.raises(RenderJobFailure) as raised:
        offline_validate(record, request, FakeInspectionTools(decode_fault=True))
    failure = raised.value
    assert failure.code == "decode-failed"
    assert failure.failure_class == "permanent"


def test_offline_metadata_mismatch_fails_typed(tmp_path: Path) -> None:
    runner, request, _project = offline_rig(tmp_path)
    record = runner.run(request)
    tools = FakeInspectionTools(audio_codec="pcm_s16le")
    with pytest.raises(RenderJobFailure) as raised:
        offline_validate(record, request, tools)
    failure = raised.value
    assert failure.code == "metadata-mismatch"
    assert failure.failure_class == "permanent"
    assert "audio_codec" in failure.detail
    assert "pcm_s16le" in failure.detail


# ---------------------------------------------------- offline: structural rule


def validated_record(**overrides: object) -> ValidatedRenderOutput:
    fields: dict[str, object] = {
        "render_job_id": "render-job-1",
        "output_path": "/renders/final.mp4",
        "output_size_bytes": len(FAKE_RENDER_BYTES),
        "output_sha256": "c" * 64,
        "ffprobe_json": json.dumps({"streams": [], "format": {}}),
        "video": FfprobeStream(codec_type="video"),
        "audio": FfprobeStream(codec_type="audio"),
        "color": RenderColorTable(),
        "decode": DecodeEvidence(
            argv=("ffmpeg", "-f", "null", "-"), exit_code=0, stderr_tail=""
        ),
        "binding": RenderOutputBinding(
            timeline_conformance_fingerprint=FINGERPRINT,
            preset_sha256="d" * 64,
            render_job_id="render-job-1",
            output_sha256="c" * 64,
        ),
    }
    fields.update(overrides)
    return ValidatedRenderOutput.model_validate(fields)


def test_validated_record_structurally_rejects_api_success_only() -> None:
    validated_record()
    with pytest.raises(ValidationError):
        validated_record(ffprobe_json="")
    with pytest.raises(ValidationError):
        DecodeEvidence.model_validate(
            {"argv": ("ffmpeg", "-f", "null", "-"), "exit_code": 1, "stderr_tail": "boom"}
        )
    with pytest.raises(ValidationError):
        validated_record(
            mismatches=(RenderMismatch(field="width", observed="640", expected="1920"),)
        )
    with pytest.raises(ValidationError):
        validated_record(
            binding=RenderOutputBinding(
                timeline_conformance_fingerprint=FINGERPRINT,
                preset_sha256="d" * 64,
                render_job_id="some-other-job",
                output_sha256="c" * 64,
            )
        )


def test_offline_allowlist_requires_declared_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        OutputAllowlist(())


# ---------------------------------------------------------------- live


@dataclass(frozen=True, slots=True)
class LiveRenderEnv:
    connection: ResolveConnection
    package: ResolvePackage
    manifest: Phase0AFixtureManifest
    ffmpeg: Path
    ffprobe: Path
    out_dir: Path


def _report_path(config: pytest.Config) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    evidence = config.getoption("--resolve-evidence")
    if evidence:
        candidate = Path(str(evidence)).resolve().parent / "resolve-host.json"
        if candidate.is_file():
            return candidate
    frozen = FROZEN_ATTEMPT_DIR / "resolve-host.json"
    if frozen.is_file():
        return frozen
    return None


def _media_bin(report_path: Path, name: str, env_key: str) -> Path:
    override = os.environ.get(env_key)
    return Path(override) if override else report_path.parent / "bootstrap/ffmpeg-7.1.1/bin" / name


@pytest.fixture(scope="session")
def live_render_env(request: pytest.FixtureRequest) -> Iterator[LiveRenderEnv]:
    report_path = _report_path(request.config)
    if report_path is None or not report_path.is_file():
        pytest.skip(
            "live requires RESOLVE_HOST_REPORT or a resolve-host.json beside the attempt"
        )
    report = load_host_report(report_path)
    try:
        with native_bridge_locale():
            connection = launch_and_connect(report)
    except BridgeConnectionError as error:
        pytest.fail(f"live Resolve bridge unavailable and not launchable: {error}")
    media_dir = Path(
        os.environ.get("FVP_BUILD_MEDIA_DIR", str(report_path.parent / "phase-0a" / "fixture"))
    )
    media = {sid: media_dir / f"{sid}.mov" for sid in ("source", "intro", "outro")}
    missing = [str(path) for path in media.values() if not path.is_file()]
    if missing:
        pytest.fail(f"live fixture media missing: {missing}")
    ffmpeg = _media_bin(report_path, "ffmpeg", "FVP_FFMPEG_BIN")
    ffprobe = _media_bin(report_path, "ffprobe", "FVP_FFPROBE_BIN")
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        pytest.fail(f"pinned ffmpeg/ffprobe missing: {ffmpeg} / {ffprobe}")
    evidence = request.config.getoption("--resolve-evidence")
    out_dir = Path(str(evidence)) / "render" if evidence else media_dir.parent / "render"
    out_dir.mkdir(parents=True, exist_ok=True)
    yield LiveRenderEnv(
        connection=connection,
        package=compile_build_package(media),
        manifest=manifest_0a(),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        out_dir=out_dir,
    )
    try:
        manager = connection.project_manager()
        for name in project_names(manager):
            if name.startswith("__fvp_test__"):
                delete_staging(manager, name)
    except (BridgeConnectionError, TypeError):
        return


def expected_render_expectation(manifest: Phase0AFixtureManifest) -> RenderPresetExpectation:
    expected = manifest.expected.render_ffprobe
    video_codec = expected.video.codec_name
    assert video_codec is not None
    format_name = expected.format.format_name
    assert format_name is not None
    return RenderPresetExpectation(
        container_format_name=format_name,
        video_codec=video_codec,
        width=expected.video.width,
        height=expected.video.height,
        r_frame_rate=expected.video.r_frame_rate,
        pix_fmt=expected.video.pix_fmt,
        audio_codec=expected.audio.codec_name,
        audio_sample_rate=int(expected.audio.sample_rate),
        audio_channels=expected.audio.channels,
        expected_nb_frames=expected.video.nb_frames,
    )


@pytest.mark.resolve_live
def test_live_render_from_built_staging_validates_and_binds(live_render_env: LiveRenderEnv) -> None:
    env = live_render_env
    package = env.package
    tools = PinnedBuildTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe)
    manager = env.connection.project_manager()
    sweep_orphan_stagings(manager)
    name = staging_project_name(package.content_hash)
    try:
        project, timeline = create_staging(manager, name, package)
        paths = verify_declared_media(package)
        pool = project.GetMediaPool()
        ensure_track_layout(timeline, package)
        media = import_media(pool, paths)
        handles = place_items(pool, package, media)
        apply_link_groups(timeline, package, handles)
        table = verify_built_conformance(timeline, package, tools.sha256)
        fingerprint = table.observed_fingerprint
        request = RenderJobRequest(
            preset=package.render_job,
            render_dir=str(env.out_dir),
            custom_name=f"fvp-final-{package.content_hash[:12]}",
            timeline_conformance_fingerprint=fingerprint,
        )
        runner = RenderJobRunner(project, OutputAllowlist((env.out_dir,)))
        record = runner.run(request)
        assert record.attempts[-1].completion_percentage == 100
        validated = validate_render_output(
            record,
            preset=package.render_job,
            expectation=expected_render_expectation(env.manifest),
            authoritative_fingerprint=fingerprint,
            tools=PinnedRenderInspectionTools(ffmpeg_bin=env.ffmpeg, ffprobe_bin=env.ffprobe),
        )
        assert validated.passed is True
        assert validated.binding.timeline_conformance_fingerprint == fingerprint
        assert validated.binding.render_job_id == record.job_id
        assert validated.binding.preset_sha256 == preset_sha256(package.render_job)
        assert validated.video.codec_name == env.manifest.expected.render_ffprobe.video.codec_name
        assert validated.video.nb_frames == env.manifest.expected.render_ffprobe.video.nb_frames
        assert validated.audio.sample_rate == env.manifest.expected.render_ffprobe.audio.sample_rate
        assert validated.decode.exit_code == 0
        assert "h264" in validated.ffprobe_json
        print(f"\nlive final render: {validated.output_path} sha256={validated.output_sha256}")
        print(f"bound timeline fingerprint: {validated.binding.timeline_conformance_fingerprint}")
        print(f"bound preset sha256: {validated.binding.preset_sha256}")
    finally:
        delete_staging(manager, name)
