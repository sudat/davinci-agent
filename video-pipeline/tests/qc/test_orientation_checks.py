"""T9 regression: technical QC must reject a rotated final render.

Reproduces the measured V44-2 blocker shape at the narrowest observable
seam (``run_qc`` over the render): with the Source Manifest / edit source /
Timeline IR bindings present, an upright render passes and a 90-degree
rotated render — carrying fully correct width/height/codec/frame-rate
metadata — is blocked by ``video_orientation_mismatch`` with the frame
observation attached. Without the bindings the run makes no orientation
claim (same coverage as before; the finishing path always binds them).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.ingest import models
from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    Eligibility,
    FileIdentity,
    RecipePointer,
    SourceManifest,
    StreamMonotonicity,
    VideoStreamRecord,
)
from services.qc.checks.orientation_checks import (
    ExpectedOrientation,
    FrameSource,
    OrientationCheckRequest,
    check_orientation,
)
from services.qc.inputs import OptionalBindings
from services.qc.issue_factory import IssueFactory
from services.qc.run import run_qc
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)
from tests.qc.support import RenderSpec, base_render, clean_policy, preset, run_ffmpeg

if TYPE_CHECKING:
    from services.qc.models import QcPolicy

RATE = RationalFrameRate(num=30, den=1)
MEZ_SECONDS = 6
#: The single kept item: record frames [0,120) come from source [60,180).
SOURCE_START_FRAME = 60
RECORD_FRAMES = 120


def _manifest_json(path: Path, *, rotation_degrees: int | None) -> Path:
    video = VideoStreamRecord(
        index=0,
        codec_type="video",
        codec_name="h264",
        time_base_num=1,
        time_base_den=30000,
        start_pts=0,
        duration_num=MEZ_SECONDS,
        duration_den=1,
        r_frame_rate_num=30,
        r_frame_rate_den=1,
        avg_frame_rate_num=30,
        avg_frame_rate_den=1,
        width=320,
        height=180,
        pix_fmt="yuv420p",
        nb_frames=MEZ_SECONDS * 30,
        rotation_degrees=rotation_degrees,
        hdr=models.HdrSignaling(
            dolby_vision_rpu=False,
            dolby_vision_profile=None,
            hdr10_mastering_display=False,
            smpte2094=False,
            color_transfer=None,
        ),
    )
    audio = AudioStreamRecord(
        index=1,
        codec_type="audio",
        codec_name="pcm_s16le",
        time_base_num=1,
        time_base_den=48000,
        start_pts=0,
        duration_num=MEZ_SECONDS,
        duration_den=1,
        sample_rate=48000,
        channels=2,
        channel_layout="stereo",
        start_offset_samples=0,
    )
    manifest = SourceManifest(
        schema_version="source-manifest-v1",
        artifact_type="source-manifest",
        artifact_id="source-manifest-t9rotation00000",
        producer=Producer(name="services.ingest", version="1"),
        content_hash="0" * 64,
        file=FileIdentity(
            path="/synthetic/t9-original.mov", size_bytes=1, sha256="a" * 64
        ),
        container=ContainerInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            format_long_name="QuickTime / MOV",
            nb_streams=2,
            duration_num=MEZ_SECONDS,
            duration_den=1,
        ),
        streams=(video, audio),
        monotonicity=(
            StreamMonotonicity(
                stream_index=0,
                monotonic=True,
                first_violation_index=None,
                sampled_packets=200,
            ),
        ),
        vfr_evidence=None,
        edit_source_recipe=RecipePointer(
            recipe_id="p0b-cfr30",
            recipe_source="config/toolchains/pins/normalize-recipes.json",
            args_sha256="b" * 64,
        ),
        eligibility=Eligibility(verdict="supported", reasons=()),
        probe=models.ProbeRecord(
            ffprobe_path="/synthetic/ffprobe",
            ffprobe_sha256="c" * 64,
            arguments=("-v", "error"),
        ),
    )
    atomic_write(path, canonical_model_bytes(manifest))
    return path


def _ir_json(path: Path) -> Path:
    source = SourceRef(
        source_id="t9-edit-source",
        span=SourceFrameSpan(
            start_frame=SOURCE_START_FRAME,
            end_frame=SOURCE_START_FRAME + RECORD_FRAMES,
            rate=RATE,
        ),
    )
    record = RecordFrameSpan(start_frame=0, end_frame=RECORD_FRAMES)
    video = TimelineItem0C(
        item_id="v-1", kind="video", source=source, record_span=record,
        av_link_id="l1", subtitle_text=None,
    )
    audio = TimelineItem0C(
        item_id="a-1", kind="audio", source=source, record_span=record,
        av_link_id="l1", subtitle_text=None,
    )
    ir = TimelineIrProduction(
        artifact_id="timeline-ir-t9-orientation",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash="0" * 64,
        producer=Producer(name="t9-orientation-test", version="1"),
        inputs=(),
        rate=RATE,
        tracks=(
            TimelineTrackProduction(track=TrackRef0C(kind="video", index=1), items=(video,)),
            TimelineTrackProduction(track=TrackRef0C(kind="audio", index=2), items=(audio,)),
        ),
    )
    atomic_write(path, canonical_model_bytes(ir))
    return path


def _policy_json(path: Path) -> Path:
    policy: QcPolicy = clean_policy(preset(RenderSpec()))
    atomic_write(path, canonical_model_bytes(policy))
    return path


def _cut_render(
    ffmpeg: Path, mezzanine: Path, out: Path, *, rotate: bool
) -> Path:
    """Cut the kept item from the mezzanine; optionally rotate 90 degrees
    clockwise and pillarbox back to 320x180 (the measured Resolve failure
    shape: fully correct stream metadata, rotated picture)."""
    start_s = SOURCE_START_FRAME / 30
    video_chain = "format=yuv420p"
    if rotate:
        video_chain = (
            "transpose=1,"
            "scale=320:180:force_original_aspect_ratio=decrease,"
            "pad=320:180:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
        )
    run_ffmpeg(
        ffmpeg,
        (
            "-ss",
            str(start_s),
            "-i",
            str(mezzanine),
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=exprs=sin(2*PI*440*t)*0.2:s=48000:d=4",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-vf",
            video_chain,
            "-frames:v",
            str(RECORD_FRAMES),
            "-r",
            "30",
            "-c:v",
            "h264_videotoolbox",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ),
        out,
    )
    return out


def _bindings(tmp_path: Path, mezzanine: Path) -> OptionalBindings:
    return OptionalBindings(
        ir=_ir_json(tmp_path / "ir.json"),
        source_manifest=_manifest_json(tmp_path / "manifest.json", rotation_degrees=None),
        edit_source=mezzanine,
    )


def test_upright_render_passes_with_orientation_bindings(
    qc_tools, tmp_path
) -> None:
    mezzanine = base_render(
        qc_tools.ffmpeg, tmp_path, RenderSpec(duration_s=MEZ_SECONDS), name="mez.mp4"
    )
    render = _cut_render(qc_tools.ffmpeg, mezzanine, tmp_path / "upright.mp4", rotate=False)
    policy = _policy_json(tmp_path / "policy.json")
    report = run_qc(
        render, policy, tmp_path / "qc.json", _bindings(tmp_path, mezzanine)
    )
    assert report.verdict == "passed", [i.detail for i in report.issues]


def test_rotated_render_is_blocked_by_orientation_mismatch(
    qc_tools, tmp_path
) -> None:
    mezzanine = base_render(
        qc_tools.ffmpeg, tmp_path, RenderSpec(duration_s=MEZ_SECONDS), name="mez.mp4"
    )
    render = _cut_render(qc_tools.ffmpeg, mezzanine, tmp_path / "rotated.mp4", rotate=True)
    policy = _policy_json(tmp_path / "policy.json")
    report = run_qc(
        render, policy, tmp_path / "qc.json", _bindings(tmp_path, mezzanine)
    )
    assert report.verdict == "blocked"
    orientation = [i for i in report.issues if i.rule_id == "video_orientation_mismatch"]
    assert orientation, [i.rule_id for i in report.issues]
    names = {m.name for issue in orientation for m in issue.evidence.measured}
    assert any(name.startswith("quarter_ncc_") for name in names)
    assert any(name.startswith("observed_quarter_") for name in names)


def test_without_bindings_no_orientation_claim_is_made(
    qc_tools, tmp_path
) -> None:
    """Documented boundary: bindings add the orientation claim; without them
    coverage is exactly the pre-T9 checks (the finishing path always binds)."""
    mezzanine = base_render(
        qc_tools.ffmpeg, tmp_path, RenderSpec(duration_s=MEZ_SECONDS), name="mez.mp4"
    )
    render = _cut_render(qc_tools.ffmpeg, mezzanine, tmp_path / "rotated.mp4", rotate=True)
    policy = _policy_json(tmp_path / "policy.json")
    report = run_qc(render, policy, tmp_path / "qc.json", OptionalBindings())
    assert report.verdict == "passed"
    assert not [i for i in report.issues if i.rule_id == "video_orientation_mismatch"]


@pytest.mark.parametrize("rotation", [90, 270, 180])
def test_expected_display_orientation_follows_manifest_rotation(
    qc_tools, tmp_path, rotation
) -> None:
    """A source recorded rotated must display rotated: the dimension part of
    the check derives portrait/landscape from manifest rotation, so an
    upright-landscape render of a rotate-90 source is a mismatch too."""
    mezzanine = base_render(
        qc_tools.ffmpeg, tmp_path, RenderSpec(duration_s=MEZ_SECONDS), name="mez.mp4"
    )
    render = _cut_render(qc_tools.ffmpeg, mezzanine, tmp_path / "upright.mp4", rotate=False)
    policy = _policy_json(tmp_path / "policy.json")
    report = run_qc(
        render,
        policy,
        tmp_path / "qc.json",
        OptionalBindings(
            ir=_ir_json(tmp_path / "ir.json"),
            source_manifest=_manifest_json(
                tmp_path / "manifest.json", rotation_degrees=rotation
            ),
            edit_source=mezzanine,
        ),
    )
    assert report.verdict == "blocked"
    orientation = [i for i in report.issues if i.rule_id == "video_orientation_mismatch"]
    assert orientation, [i.rule_id for i in report.issues]



def _rational_ir(ir_rate: RationalFrameRate, source_rate: RationalFrameRate):
    source = SourceRef(
        source_id="t9-edit-source",
        span=SourceFrameSpan(
            start_frame=SOURCE_START_FRAME,
            end_frame=SOURCE_START_FRAME + RECORD_FRAMES,
            rate=source_rate,
        ),
    )
    record = RecordFrameSpan(start_frame=0, end_frame=RECORD_FRAMES)
    video = TimelineItem0C(
        item_id="v-1", kind="video", source=source, record_span=record,
        av_link_id="l1", subtitle_text=None,
    )
    audio = TimelineItem0C(
        item_id="a-1", kind="audio", source=source, record_span=record,
        av_link_id="l1", subtitle_text=None,
    )
    return TimelineIrProduction(
        artifact_id="timeline-ir-t9-rational",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash="0" * 64,
        producer=Producer(name="t9-orientation-test", version="1"),
        inputs=(),
        rate=ir_rate,
        tracks=(
            TimelineTrackProduction(track=TrackRef0C(kind="video", index=1), items=(video,)),
            TimelineTrackProduction(track=TrackRef0C(kind="audio", index=2), items=(audio,)),
        ),
    )


def _recording_frame_source(ramp: list[float]) -> tuple[FrameSource, list[tuple[str, float]]]:
    requested: list[tuple[str, float]] = []

    def read(media: Path, at_seconds: float) -> list[float]:
        requested.append((media.name, at_seconds))
        return ramp

    return read, requested


NTSC = RationalFrameRate(num=30000, den=1001)


@pytest.mark.parametrize(
    ("ir_rate", "source_rate", "expected_record_s", "expected_source_s"),
    [
        (NTSC, NTSC, 60 * 1001 / 30000, 120 * 1001 / 30000),
        (NTSC, RATE, 60 * 1001 / 30000, 120 / 30),
        (RATE, NTSC, 60 / 30, 120 * 1001 / 30000),
    ],
)
def test_rational_rate_requests_exact_seconds_for_record_and_source(
    ir_rate, source_rate, expected_record_s, expected_source_s
) -> None:
    """30000/1001 must seek at frame*1001/30000 seconds (verified defect:
    frame/30000 sampled ~1001x too early); record seconds follow the IR rate
    and source seconds follow the item's span rate."""
    ramp = [float(index % 251) for index in range(96 * 54)]
    frame_source, requested = _recording_frame_source(ramp)
    request = OrientationCheckRequest(
        render=Path("render.mp4"),
        report=FfprobeReport(
            streams=(FfprobeStream(codec_type="video", width=320, height=180),),
            format=FfprobeFormat(),
        ),
        render_streams_raw=(),
        expectation=ExpectedOrientation(
            rotation_degrees=0, source_width=320, source_height=180
        ),
        ir=_rational_ir(ir_rate, source_rate),
        edit_source=Path("mez.mov"),
        frame_source=frame_source,
        factory=IssueFactory.for_policy(
            clean_policy(preset()), ("0" * 64,)
        ),
    )
    issues = check_orientation(request)
    assert issues == ()
    assert requested == [
        ("render.mp4", pytest.approx(expected_record_s, abs=1e-9)),
        ("mez.mov", pytest.approx(expected_source_s, abs=1e-9)),
    ]


def _three_item_ir() -> TimelineIrProduction:
    items = []
    for index, (start, end) in enumerate(((0, 60), (60, 90), (90, 180))):
        span = SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE)
        record = RecordFrameSpan(start_frame=start, end_frame=end)
        items.append(
            TimelineItem0C(
                item_id=f"v-{index}", kind="video",
                source=SourceRef(source_id="t9-edit-source", span=span),
                record_span=record, av_link_id=f"l{index}", subtitle_text=None,
            )
        )
    return TimelineIrProduction(
        artifact_id="timeline-ir-t9-multi", artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1", content_hash="0" * 64,
        producer=Producer(name="t9-orientation-test", version="1"), inputs=(),
        rate=RATE,
        tracks=(
            TimelineTrackProduction(
                track=TrackRef0C(kind="video", index=1), items=tuple(items)
            ),
        ),
    )


def _conclusive_checker_pattern() -> list[float]:
    return [float((index // 4) % 2) * 180.0 for index in range(96 * 54)]


def _weak_noise_pattern() -> list[float]:
    return [float((index * 37) % 251) for index in range(96 * 54)]


def _request_with_frame_source(
    frame_source: FrameSource, ir: TimelineIrProduction
) -> OrientationCheckRequest:
    return OrientationCheckRequest(
        render=Path("render.mp4"),
        report=FfprobeReport(
            streams=(FfprobeStream(codec_type="video", width=320, height=180),),
            format=FfprobeFormat(),
        ),
        render_streams_raw=(),
        expectation=ExpectedOrientation(
            rotation_degrees=0, source_width=320, source_height=180
        ),
        ir=ir,
        edit_source=Path("mez.mov"),
        frame_source=frame_source,
        factory=IssueFactory.for_policy(clean_policy(preset()), ("0" * 64,)),
    )


def test_one_dominant_upright_sample_passes_despite_weak_samples() -> None:
    """Calibrated semantics (measured on the representative episode): a
    blurred/dark sample is noise, never a positive rotation observation;
    one dominant upright sample proves the global layer orientation."""
    ir = _three_item_ir()
    ramp = _conclusive_checker_pattern()
    weak = _weak_noise_pattern()

    def frames(media: Path, at_seconds: float) -> list[float]:
        if media.name == "render.mp4":
            return ramp if at_seconds >= 4.0 else weak
        return ramp if at_seconds >= 4.0 else weak

    issues = check_orientation(_request_with_frame_source(frames, ir))
    assert issues == ()


def test_no_dominant_sample_blocks_fail_closed() -> None:
    """Zero conclusive samples cannot verify orientation: blocked."""
    ir = _three_item_ir()
    source = _conclusive_checker_pattern()
    render_noise = _weak_noise_pattern()

    def frames(media: Path, at_seconds: float) -> list[float]:
        return source if media.name == "mez.mov" else render_noise

    issues = check_orientation(_request_with_frame_source(frames, ir))
    assert [i.rule_id for i in issues] == ["video_orientation_unverified"]
