"""Deliver-page render execution and the external mov_text subtitle pairing.

Render completion is detected ONLY via the frozen
``CompletionPercentage == 100`` rule (localized job-status strings are never
parsed) with a bounded poll; MarkIn/MarkOut never bound the extent because
``SelectAllFrames`` is always set. The subtitle step never places anything
in the timeline: the pinned ffmpeg muxes the package's cues (rendered to an
SRT) into the finished render as a mov_text stream, exactly the verified
external rung.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import (
    BuildFailure,
    BuildTools,
    RenderResult,
    RenderTiming,
    SubtitleMuxJob,
    SubtitleResult,
    summarize_probe,
)
from services.foundation_io import atomic_write
from services.resolve_bridge.fixed_presentation_tools import MediaTools
from services.toolchain.render_qc import render_complete

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage, SubtitleCueInstruction
    from services.resolve_bridge.fixed_presentation_models import (
        FfprobeReport,
        FixedProjectApi,
    )

MUX_TIMEOUT_SECONDS: Final = 300
PLACEHOLDER_PATTERN: Final = re.compile(r"^\{[a-z]+\}$")
SUBTITLE_PLACEHOLDERS: Final = frozenset({"{ffmpeg}", "{render}", "{srt}", "{output}"})
SUBTITLE_CODEC: Final = "mov_text"
SRT_NAME: Final = "subtitle.srt"


def render_output_custom_name(package: ResolvePackage) -> str:
    return f"fvp-build-{package.content_hash[:12]}"


def render_timeline(
    project: FixedProjectApi,
    package: ResolvePackage,
    render_dir: Path,
    tools: BuildTools,
    timing: RenderTiming,
) -> RenderResult:
    spec = package.render_job
    render_dir.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec(spec.video_format, spec.video_codec):
        raise BuildFailure(
            "render-setup-failed",
            f"SetCurrentRenderFormatAndCodec({spec.video_format},{spec.video_codec}) failed",
        )
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": render_output_custom_name(package),
        "FormatWidth": spec.width,
        "FormatHeight": spec.height,
        "FrameRate": spec.frame_rate.num,
        "AudioCodec": spec.audio_codec,
        "AudioSampleRate": spec.audio_sample_rate,
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise BuildFailure("render-setup-failed", "SetRenderSettings failed")
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise BuildFailure("render-job-failed", "AddRenderJob returned no job id")
    entry = _job_entry(project, job_id)
    if not project.StartRendering(job_id):
        raise BuildFailure("render-start-failed", f"StartRendering failed for {job_id}")
    completed = False
    polls = 0
    seen = -1
    deadline = time.monotonic() + timing.deadline_seconds
    while time.monotonic() < deadline:
        status = project.GetRenderJobStatus(job_id)
        polls += 1
        percentage = status.get("CompletionPercentage")
        if isinstance(percentage, int) and not isinstance(percentage, bool):
            seen = percentage
        if render_complete(status):
            completed = True
            break
        timing.sleep(timing.poll_seconds)
    if not completed:
        project.StopRendering()
        raise BuildFailure(
            "render-incomplete",
            f"render job {job_id} did not reach CompletionPercentage==100 within "
            f"{timing.deadline_seconds:.0f}s (last seen {seen} after {polls} polls)",
        )
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise BuildFailure("render-output-missing", f"completed render has no output: {output}")
    return RenderResult(
        job_id=job_id,
        output_path=str(output),
        output_sha256=tools.sha256(output),
        completion_percentage=100,
        poll_count=polls,
        probe=summarize_probe(tools.probe(output)),
    )


def _job_entry(project: FixedProjectApi, job_id: str) -> dict[str, object]:
    for entry in project.GetRenderJobList():
        if entry.get("JobId") == job_id:
            return entry
    raise BuildFailure("render-job-failed", f"render job {job_id} missing from GetRenderJobList")


def _timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_srt_bytes(cues: tuple[SubtitleCueInstruction, ...]) -> bytes:
    lines: list[str] = []
    for index, cue in enumerate(sorted(cues, key=lambda c: (c.start_ms, c.cue_id)), 1):
        lines.append(str(index))
        lines.append(f"{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}")
        lines.extend(cue.lines)
        lines.append("")
    return "\n".join(lines).encode()


def run_subtitle_step(
    tools: BuildTools,
    ffmpeg_bin: Path,
    package: ResolvePackage,
    render_output: Path,
    work_dir: Path,
) -> SubtitleResult:
    step = package.subtitle_step
    if step is None:
        raise BuildFailure("subtitle-step-failed", "no subtitle step to run")
    work_dir.mkdir(parents=True, exist_ok=True)
    srt_path = work_dir / SRT_NAME
    atomic_write(srt_path, render_srt_bytes(step.cues))
    output = render_output.with_name(render_output.stem + "-subtitled" + render_output.suffix)
    tools.mux_subtitles(
        ffmpeg_bin, step.argv, SubtitleMuxJob(render=render_output, srt=srt_path, output=output)
    )
    if not output.is_file():
        raise BuildFailure("subtitle-step-failed", f"mux produced no output: {output}")
    probe = tools.probe(output)
    codec = next((s.codec_name for s in probe.streams if s.codec_type == "subtitle"), None)
    if codec != SUBTITLE_CODEC:
        raise BuildFailure(
            "subtitle-step-failed",
            f"paired artifact subtitle codec {codec!r} != {SUBTITLE_CODEC!r}",
        )
    return SubtitleResult(
        srt_sha256=tools.sha256(srt_path),
        output_path=str(output),
        output_sha256=tools.sha256(output),
        codec=SUBTITLE_CODEC,
    )


class PinnedBuildTools:
    """BuildTools over the pinned ffmpeg/ffprobe binaries; mux honors the package argv."""

    def __init__(self, *, ffmpeg_bin: Path, ffprobe_bin: Path) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._tools = MediaTools(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)

    def probe(self, path: Path) -> FfprobeReport:
        return self._tools.probe(path)

    def sha256(self, path: Path) -> str:
        return self._tools.sha256(path)

    def mux_subtitles(
        self, ffmpeg: Path, argv: tuple[str, ...], job: SubtitleMuxJob
    ) -> None:
        del ffmpeg  # the pinned binary from construction is authoritative
        substitutions = {
            "{ffmpeg}": str(self._ffmpeg_bin),
            "{render}": str(job.render),
            "{srt}": str(job.srt),
            "{output}": str(job.output),
        }
        for token in argv:
            if PLACEHOLDER_PATTERN.match(token) and token not in substitutions:
                raise BuildFailure(
                    "subtitle-step-failed", f"argv carries an unknown placeholder {token}"
                )
        if "-nostdin" not in argv:
            raise BuildFailure("subtitle-step-failed", "subtitle argv must stay non-interactive")
        final = [substitutions.get(token, token) for token in argv]
        if final[0] != str(self._ffmpeg_bin):
            raise BuildFailure("subtitle-step-failed", "argv must invoke the pinned ffmpeg first")
        try:
            result = subprocess.run(
                final,
                check=False,
                capture_output=True,
                text=True,
                timeout=MUX_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise BuildFailure(
                "subtitle-step-failed", f"ffmpeg mux exceeded {MUX_TIMEOUT_SECONDS}s"
            ) from error
        if result.returncode != 0 or not job.output.is_file():
            raise BuildFailure(
                "subtitle-step-failed", f"ffmpeg mov_text mux failed: {result.stderr.strip()}"
            )


__all__ = [
    "PinnedBuildTools",
    "render_output_custom_name",
    "render_srt_bytes",
    "render_timeline",
    "run_subtitle_step",
]
