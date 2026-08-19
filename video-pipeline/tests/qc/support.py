"""Todo-52 QC rig: pinned tools, deterministic render synthesis, policies.

Renders are synthesized by the pinned ffmpeg (lavfi testsrc2/color sources,
aevalsrc audio, mov_text subtitle mux) so every fault fixture is a REAL
media file whose decode/measurement the engine must catch. Policies are
authored by the deterministic builder or assembled directly in tests.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, Literal

from services.build.render_models import RenderPresetExpectation
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.foundation_io import canonical_model_bytes
from services.qc.models import (
    AudioThresholds,
    IrThresholds,
    PreviewThresholds,
    QcPolicy,
    SubtitleThresholds,
    VideoThresholds,
)

IR_PRODUCER: Final = Producer(name="todo52-qc-rig", version="1")
STYLE_REF: Final = "qc-style"
BLACK_SOURCES: Final[tuple[str, ...]] = (
    "color=c=0x808080:s=320x180:r=30:d=1.5",
    "color=c=black:s=320x180:r=30:d=2.5",
    "testsrc2=s=320x180:r=30:d=1",
)
FREEZE_SOURCES: Final[tuple[str, ...]] = (
    "color=c=0x808080:s=320x180:r=30:d=3",
    "testsrc2=s=320x180:r=30:d=1",
)


def run_ffmpeg(ffmpeg: Path, argv: tuple[str, ...], out: Path) -> Path:
    result = subprocess.run(
        (str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert out.is_file()
    return out


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """One deterministic render recipe; multiple video sources concatenate."""

    width: int = 320
    height: int = 180
    rate: int = 30
    duration_s: int = 4
    audio_expr: str = "sin(2*PI*440*t)*0.2"
    audio_channels: int = 2
    video_sources: tuple[str, ...] = ()
    srt_lines: tuple[tuple[int, int, str], ...] = ()

    def sources(self) -> tuple[str, ...]:
        if self.video_sources:
            return self.video_sources
        return (f"testsrc2=s={self.width}x{self.height}:r={self.rate}:d={self.duration_s}",)


def base_render(
    ffmpeg: Path,
    out_dir: Path,
    spec: RenderSpec,
    name: str = "render.mp4",
) -> Path:
    sources = spec.sources()
    argv: list[str] = []
    for source in sources:
        argv.extend(("-f", "lavfi", "-i", source))
    argv.extend(
        (
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs={spec.audio_expr}:s=48000:d={spec.duration_s}",
        )
    )
    if len(sources) == 1:
        argv.extend(("-map", "0:v", "-map", "1:a", "-vf", "format=yuv420p"))
    else:
        chain = "".join(f"[{index}:v]" for index in range(len(sources)))
        argv.extend(
            (
                "-filter_complex",
                f"{chain}concat=n={len(sources)}:v=1:a=0,format=yuv420p[v]",
                "-map",
                "[v]",
                "-map",
                f"{len(sources)}:a",
            )
        )
    argv.extend(
        (
            "-r",
            str(spec.rate),
            "-c:v",
            "h264_videotoolbox",
            "-ac",
            str(spec.audio_channels),
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-shortest",
        )
    )
    out = out_dir / name
    run_ffmpeg(ffmpeg, (*argv, str(out)), out)
    if spec.srt_lines:
        srt = out_dir / f"{name}.srt"
        blocks = []
        for index, (start, end, text) in enumerate(spec.srt_lines):
            blocks.append(
                f"{index + 1}\n{_stamp(start)} --> {_stamp(end)}\n{text}\n"
            )
        srt.write_bytes("\n".join(blocks).encode())
        paired = out_dir / f"sub-{name}"
        run_ffmpeg(
            ffmpeg,
            (
                "-i",
                str(out),
                "-i",
                str(srt),
                "-map",
                "0:v",
                "-map",
                "0:a",
                "-map",
                "1:0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-c:s",
                "mov_text",
                str(paired),
            ),
            paired,
        )
        return paired
    return out


def _stamp(ms: int) -> str:
    hours, rem = divmod(ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def clean_policy(
    expectation: RenderPresetExpectation, *, subtitle_required: bool = False
) -> QcPolicy:
    policy = QcPolicy(
        schema_version="resolved-qc-policy-v1",
        threshold_version="qc-thresholds-test-v1",
        subtitle=SubtitleThresholds(
            min_duration_frames=15,
            max_lines=2,
            max_chars_per_line=42,
            timing_tolerance_ms=40,
            track_required=subtitle_required,
        ),
        video=VideoThresholds(
            black_min_duration_ms=1000,
            freeze_min_duration_ms=2000,
            expectation=expectation,
        ),
        audio=AudioThresholds(
            max_peak_mb=-1000,
            loudness_min_mlufs=-40000,
            loudness_max_mlufs=-5000,
            max_silence_ms=2000,
            expected_channels=2,
        ),
        preview=PreviewThresholds(
            require_binding=False,
            subtitle_expected=False,
            max_duration_drift_ms=50,
        ),
        ir=IrThresholds(require_binding=False, expected_total_frames=None),
        required_capabilities=(),
        capability_matrix=None,
        policy_sha256="0" * 64,
    )
    return policy.model_copy(update={"policy_sha256": policy.content_hash()})


def preset(
    spec: RenderSpec | None = None, nb_frames: str | None = None
) -> RenderPresetExpectation:
    resolved = spec if spec is not None else RenderSpec()
    return RenderPresetExpectation(
        container_format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec="h264",
        width=resolved.width,
        height=resolved.height,
        r_frame_rate=f"{resolved.rate}/1",
        pix_fmt="yuv420p",
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=resolved.audio_channels,
        expected_nb_frames=nb_frames,
    )


def av_item(
    item_id: str,
    kind: Literal["video", "audio"],
    link: str,
    start: int,
    end: int,
) -> TimelineItem0C:
    return TimelineItem0C(
        item_id=item_id,
        kind=kind,
        source=SourceRef(
            source_id=f"src-{link}",
            span=SourceFrameSpan(start_frame=0, end_frame=end - start, rate=RATE),
        ),
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
        av_link_id=link,
        subtitle_text=None,
    )


RATE: Final = RationalFrameRate(num=30, den=1)


def timeline_ir(
    spans: tuple[tuple[int, int], ...],
    *,
    audio_spans: tuple[tuple[int, int], ...] | None = None,
    cue_spans: tuple[tuple[int, int, str], ...] = (),
    gap_audio: bool = False,
) -> TimelineIrProduction:
    video = tuple(av_item(f"v-{index}", "video", f"l{index}", start, end)
                  for index, (start, end) in enumerate(spans))
    resolved_audio = spans if audio_spans is None else audio_spans
    audio = tuple(av_item(f"a-{index}", "audio", f"l{index}", start, end)
                  for index, (start, end) in enumerate(resolved_audio))
    if gap_audio:
        audio = audio[:-1]
    tracks = [
        TimelineTrackProduction(track=TrackRef0C(kind="video", index=1), items=video),
        TimelineTrackProduction(track=TrackRef0C(kind="audio", index=2), items=audio),
    ]
    if cue_spans:
        cues = tuple(
            SubtitleCueItem(
                item_id=f"cue-{index}",
                source=SourceRef(
                    source_id="subtitle",
                    span=SourceFrameSpan(start_frame=0, end_frame=end - start, rate=RATE),
                ),
                record_span=RecordFrameSpan(start_frame=start, end_frame=end),
                text=text,
                lines=(text,),
                style_ref=STYLE_REF,
                safe_area=True,
                min_duration_frames=15,
            )
            for index, (start, end, text) in enumerate(cue_spans)
        )
        tracks.append(
            TimelineTrackProduction(track=TrackRef0C(kind="subtitle", index=3), items=cues)
        )
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(RATE))
    for track in tracks:
        digest.update(canonical_model_bytes(track))
    return TimelineIrProduction(
        artifact_id="timeline-ir-qc-test",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash=digest.hexdigest(),
        producer=IR_PRODUCER,
        inputs=(),
        rate=RATE,
        tracks=tuple(tracks),
    )


def rehash(policy: QcPolicy) -> QcPolicy:
    """Re-derive the content hash after a model_copy update."""
    return policy.model_copy(update={"policy_sha256": policy.content_hash()})


def rate_ms(rate: RationalFrameRate) -> Fraction:
    return Fraction(rate.den * 1000, rate.num)


__all__ = [
    "BLACK_SOURCES",
    "FREEZE_SOURCES",
    "RATE",
    "RenderSpec",
    "av_item",
    "base_render",
    "clean_policy",
    "preset",
    "rate_ms",
    "rehash",
    "timeline_ir",
]
