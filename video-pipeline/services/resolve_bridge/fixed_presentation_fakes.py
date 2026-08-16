"""In-memory fakes for the fixed-presentation fault harness.

The fakes mirror the timeline start-timecode, track-layout, subtitle-refusal,
render-job, and media-tool surfaces the fixed-presentation orchestration uses,
so injected faults exercise the real ladder and verification code offline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.resolve_bridge.base_cut_fakes import (
    FakeBaseCutMediaPool,
    FakeBaseCutPoolItem,
    FakeBaseCutTimeline,
)
from services.resolve_bridge.base_cut_faults import (
    FakeBaseCutManager,
    FakeBaseCutProject,
)
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)
from services.resolve_bridge.fixed_presentation_tools import (
    DecodeOutcome,
    RenderError,
    SubtitlePacket,
)

if TYPE_CHECKING:
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolItemApi,
        BaseCutTimelineItemApi,
    )

MISSING_AUDIO_RECORD_FRAME = 330 + 108000
AUDIO_MEDIA_TYPE = 2
FAKE_RENDER_MAGIC = b"fake-render-output"


class FakeFpTimeline(FakeBaseCutTimeline):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._start_tc = "01:00:00:00"

    def SetStartTimecode(self, timecode: str) -> bool:
        self._start_tc = timecode
        return True

    def GetStartTimecode(self) -> str:
        return self._start_tc

    def AddTrack(self, track_type: str, sub_track_type: str = "") -> bool:
        return super().AddTrack(track_type)

    def GetStartFrame(self) -> int:
        return min((item.GetStart(False) for item in self._items), default=0)

    def GetEndFrame(self) -> int:
        return max((item.GetEnd(False) for item in self._items), default=0)


class FakeFpMediaPool(FakeBaseCutMediaPool):
    def __init__(self, fault: str = "") -> None:
        super().__init__(fault)
        self._fp_fault = fault
        self._srt_items: set[str] = set()

    def CreateEmptyTimeline(self, name: str) -> FakeFpTimeline:
        timeline = FakeFpTimeline(name)
        self._timeline = timeline
        return timeline

    def ImportMedia(self, paths: list[str]) -> list[BaseCutMediaPoolItemApi]:
        imported: list[BaseCutMediaPoolItemApi] = []
        for path in paths:
            if path.endswith(".srt"):
                self._srt_items.add(path)
                imported.append(FakeBaseCutPoolItem(path))
            else:
                imported.append(self._pool_item(path))
        return imported

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[BaseCutTimelineItemApi]:
        if any(self._is_srt(info) for info in clip_infos):
            return []
        added = super().AppendToTimeline(clip_infos)
        if self._fp_fault == "missing_item":
            for info, item in zip(clip_infos, added, strict=True):
                if self._is_missing_item_target(info):
                    timeline = self._timeline
                    if timeline is not None:
                        timeline.drop_item(item.GetUniqueId())
        return added

    def _is_missing_item_target(self, info: dict[str, object]) -> bool:
        record = info.get("recordFrame")
        return info.get("mediaType") == AUDIO_MEDIA_TYPE and record == MISSING_AUDIO_RECORD_FRAME

    def _is_srt(self, info: dict[str, object]) -> bool:
        raw = info.get("mediaPoolItem")
        if not isinstance(raw, FakeBaseCutPoolItem):
            return False
        path = raw.GetClipProperty("File Path")
        return isinstance(path, str) and path in self._srt_items


class FakeFpProject(FakeBaseCutProject):
    def __init__(self, name: str, pool: FakeFpMediaPool, render_dir: Path) -> None:
        super().__init__(name, pool)
        self._render_dir = render_dir
        self._jobs: list[dict[str, object]] = []
    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool:
        return True

    def SetRenderSettings(self, settings: dict[str, object]) -> bool:
        return True

    def AddRenderJob(self) -> str:
        job_id = f"fake-job-{len(self._jobs) + 1}"
        output = self._render_dir / "fixed-presentation.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-render-output")
        self._jobs.append(
            {
                "JobId": job_id,
                "TargetDir": str(self._render_dir),
                "OutputFilename": output.name,
                "MarkIn": 108000,
                "MarkOut": 108659,
            }
        )
        return job_id

    def StartRendering(self, job_id: str) -> bool:
        return True

    def StopRendering(self) -> None:
        return None

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]:
        return {"JobStatus": "Complete", "CompletionPercentage": 100, "TimeTakenToRenderInMs": 1}

    def GetRenderJobList(self) -> list[dict[str, object]]:
        return list(self._jobs)


class FakeFpManager(FakeBaseCutManager):
    def __init__(self, render_dir: Path, fault: str = "") -> None:
        super().__init__("")
        self._render_dir = render_dir
        self._fault = fault

    def CreateProject(self, project_name: str) -> FakeFpProject:
        self._names.add(project_name)
        project = FakeFpProject(project_name, FakeFpMediaPool(self._fault), self._render_dir)
        self._projects[project_name] = project
        self._current = project
        return project


class FakeTools:
    def __init__(self, fault: str, srt_bytes: bytes) -> None:
        self._fault = fault
        self._srt = srt_bytes

    def probe(self, path: Path) -> FfprobeReport:
        if path.name.endswith("-subtitled.mp4"):
            return FfprobeReport(
                streams=(FfprobeStream(codec_type="subtitle", codec_name="mov_text"),),
                format=FfprobeFormat(format_name="mov,mp4,m4a,3gp,3g2,mj2", duration="22.080000"),
            )
        audio = FfprobeStream(
            codec_type="audio",
            codec_name="aac",
            sample_rate="48000",
            channels=2,
            duration="22.080000",
        )
        if self._fault == "preset_mismatch":
            audio = audio.model_copy(update={"codec_name": "pcm_s16le", "channels": 1})
        video = FfprobeStream(
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1080,
            pix_fmt="yuv420p",
            r_frame_rate="30/1",
            avg_frame_rate="30/1",
            nb_frames="660",
            duration="22.000000",
        )
        return FfprobeReport(
            streams=(video, audio),
            format=FfprobeFormat(format_name="mov,mp4,m4a,3gp,3g2,mj2", duration="22.080000"),
        )

    def mux_subtitle(self, render_output: Path, srt_path: Path, paired: Path) -> None:
        if self._fault == "missing_fallback":
            raise RenderError("ffmpeg mov_text mux failed (missing fallback fault)")
        paired.write_bytes(b"fake-paired")

    def demux_subtitle(self, path: Path) -> bytes:
        return self._srt

    def subtitle_packets(self, path: Path) -> tuple[SubtitlePacket, ...]:
        return (SubtitlePacket(pts=0.0, duration=6.0), SubtitlePacket(pts=6.0, duration=2.0))

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def decode(self, path: Path) -> DecodeOutcome:
        argv = (f"ffmpeg({path.name})", "-f", "null")
        try:
            decodable = path.read_bytes().startswith(FAKE_RENDER_MAGIC)
        except OSError:
            decodable = False
        return DecodeOutcome(
            argv=argv,
            exit_code=0 if decodable else 1,
            stderr_tail="" if decodable else "corrupt render bytes",
        )


