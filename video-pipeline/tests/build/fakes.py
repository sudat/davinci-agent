"""Offline fault rig for the Todo-48 Clean Builder (Todo 15/16 fakes pattern).

Extends the in-memory Resolve fakes with exactly two builder-relevant fault
knobs: a placed-item drop between placement and readback (the inherited
``missing_item`` pool fault) and a render job that reports a localized
"Complete" status string with a percentage below 100 (the false-render-
complete trap). The render output name always comes from the job-list
entry, mirroring the live Deliver page.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.fixed_presentation_fakes import (
    FakeFpManager,
    FakeFpMediaPool,
    FakeFpProject,
)
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)

if TYPE_CHECKING:
    from services.build.builder_models import BuildTools, SubtitleMuxJob

RENDER_INCOMPLETE: Final = "render_incomplete"


class FakeBuildProject(FakeFpProject):
    """Renders a stalled status payload when the fault is armed."""

    def __init__(
        self, name: str, pool: FakeFpMediaPool, render_dir: Path, status_fault: str
    ) -> None:
        super().__init__(name, pool, render_dir)
        self._status_fault = status_fault

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]:  # noqa: N802 (Resolve API)
        if self._status_fault == RENDER_INCOMPLETE:
            return {"JobStatus": "Complete", "CompletionPercentage": 99, "TimeTakenToRenderInMs": 1}
        return super().GetRenderJobStatus(job_id)


class FakeBuildManager(FakeFpManager):
    """Creates build projects whose render status honors the armed fault."""

    def __init__(self, render_dir: Path, fault: str = "", status_fault: str = "") -> None:
        super().__init__(render_dir, fault)
        self._status_fault = status_fault

    def CreateProject(self, project_name: str) -> FakeBuildProject:  # noqa: N802 (Resolve API)
        self._names.add(project_name)
        project = FakeBuildProject(
            project_name, FakeFpMediaPool(self._fault), self._render_dir, self._status_fault
        )
        self._projects[project_name] = project
        self._current = project
        return project


class FakeBuildTools:
    """In-memory BuildTools: canned probes and a placeholder-honoring mux."""

    def __init__(self) -> None:
        self._subtitled: set[Path] = set()
        self.mux_calls: list[tuple[str, ...]] = []

    def probe(self, path: Path) -> FfprobeReport:
        video = FfprobeStream(
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1080,
            pix_fmt="yuv420p",
            r_frame_rate="30/1",
            avg_frame_rate="30/1",
            nb_frames="660",
        )
        audio = FfprobeStream(
            codec_type="audio", codec_name="aac", sample_rate="48000", channels=2
        )
        streams: tuple[FfprobeStream, ...] = (video, audio)
        if path in self._subtitled:
            streams = (video, audio, FfprobeStream(codec_type="subtitle", codec_name="mov_text"))
        return FfprobeReport(
            streams=streams,
            format=FfprobeFormat(format_name="mov,mp4,m4a,3gp,3g2,mj2", duration="22.0"),
        )

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def mux_subtitles(
        self,
        ffmpeg: Path,
        argv: tuple[str, ...],
        job: SubtitleMuxJob,
    ) -> None:
        del ffmpeg
        self.mux_calls.append(argv)
        assert "-nostdin" in argv, "subtitle argv must stay non-interactive"
        for placeholder in ("{ffmpeg}", "{render}", "{srt}", "{output}"):
            assert placeholder in argv, f"subtitle argv lost {placeholder}"
        job.output.parent.mkdir(parents=True, exist_ok=True)
        job.output.write_bytes(b"fake-subtitled|" + job.srt.read_bytes())
        self._subtitled.add(job.output)


def fake_build_tools() -> BuildTools:
    return FakeBuildTools()
