"""Value models, protocols, and typed failures for the single-writer Clean Builder.

The builder promotes ONLY the live-verified Resolve bridge operations: a
fresh disposable staging project per build, MediaPool import, clipInfo
placement at timeline-absolute record frames at/above the 01:00:00:00
origin, A/V link groups, the Deliver-page render bound to the frozen
CompletionPercentage==100 rule with SelectAllFrames, and the external
post-render mov_text subtitle pairing. Item spans are read back as
start+duration because the API's end-frame accessor carries a live-verified
float-floor artifact; that accessor is intentionally absent here. The
builder NEVER writes Job State: the exclusive lease, the package registry,
and the media tools are injected by the caller, and the builder returns
artifacts only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING, Final, Protocol

from services.build.conformance_models import (
    ConformanceTable,  # noqa: TC001 (pydantic resolves annotations at runtime)
)
from services.contracts.primitives import Sha256, StrictModel

if TYPE_CHECKING:
    from services.resolve_bridge.fixed_presentation_models import FfprobeReport

KIND_RANK: Final[dict[str, int]] = {"video": 0, "audio": 1}


class BuildFailure(Exception):  # noqa: N818 (typed-refusal vocabulary, not an error kind)
    """A typed refusal: the build cannot proceed or did not conform."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class BuildInterrupted(Exception):  # noqa: N818 (kill semantics, not an error kind)
    """A simulated mid-build kill; recovery restarts fresh and never resumes."""


class LeaseHeld(Exception):  # noqa: N818 (lease state, not an error kind)
    """The exclusive Resolve build lease is held by another builder."""


class BuildLease(Protocol):
    """The exclusive mutation lease; acquire refuses while another holder owns it."""

    def acquire(self) -> None: ...

    def release(self) -> None: ...


class BuildSeams(Protocol):
    """Observation seams between build phases (kill injection and test hooks)."""

    def after_place(self) -> None: ...

    def after_readback(self) -> None: ...


class NoopSeams:
    """Default seams: no observation, no interruption."""

    def after_place(self) -> None:
        return None

    def after_readback(self) -> None:
        return None


class PackageRegistry(Protocol):
    """The compiled-package registry the builder checks packages against."""

    def expected_hash(self, artifact_id: str) -> Sha256 | None: ...


@dataclass(frozen=True, slots=True)
class SubtitleMuxJob:
    """One external subtitle pairing: render + srt in, paired output out."""

    render: Path
    srt: Path
    output: Path


class BuildTools(Protocol):
    """Pinned ffmpeg/ffprobe surface the render and subtitle steps use."""

    def probe(self, path: Path) -> FfprobeReport: ...

    def sha256(self, path: Path) -> str: ...

    def mux_subtitles(
        self, ffmpeg: Path, argv: tuple[str, ...], job: SubtitleMuxJob
    ) -> None: ...


class RenderTiming:
    """Bounded render polling; the sleeper is injectable for offline speed."""

    def __init__(self, deadline_seconds: float = 900.0, poll_seconds: float = 2.0) -> None:
        self.deadline_seconds = deadline_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep


class BuilderWiring:
    """Injected collaborators; the builder owns none of these authorities."""

    def __init__(  # noqa: PLR0913 (wiring record: one slot per injected authority)
        self,
        *,
        registry: PackageRegistry,
        tools: BuildTools,
        ffmpeg_bin: Path,
        render_dir: Path,
        evidence_bundle: Path | None = None,
        seams: BuildSeams | None = None,
        timing: RenderTiming | None = None,
        prior_conformance_fingerprint: str | None = None,
    ) -> None:
        self.registry = registry
        self.tools = tools
        self.ffmpeg_bin = ffmpeg_bin
        self.render_dir = render_dir
        self.evidence_bundle = evidence_bundle
        self.seams = seams if seams is not None else NoopSeams()
        self.timing = timing if timing is not None else RenderTiming()
        self.prior_conformance_fingerprint = prior_conformance_fingerprint


class FfprobeSummary(StrictModel):
    format_name: str | None
    duration: str | None
    video_codec: str | None
    width: int | None
    height: int | None
    r_frame_rate: str | None
    nb_frames: str | None
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None


def summarize_probe(report: FfprobeReport) -> FfprobeSummary:
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    return FfprobeSummary(
        format_name=report.format.format_name,
        duration=report.format.duration,
        video_codec=video.codec_name if video else None,
        width=video.width if video else None,
        height=video.height if video else None,
        r_frame_rate=video.r_frame_rate if video else None,
        nb_frames=video.nb_frames if video else None,
        audio_codec=audio.codec_name if audio else None,
        audio_sample_rate=int(audio.sample_rate) if audio and audio.sample_rate else None,
        audio_channels=audio.channels if audio else None,
    )


class RenderResult(StrictModel):
    job_id: str
    output_path: str
    output_sha256: Sha256
    completion_percentage: int
    poll_count: int
    probe: FfprobeSummary


class SubtitleResult(StrictModel):
    srt_sha256: Sha256
    output_path: str
    output_sha256: Sha256
    codec: str


class ItemReadbackRow(StrictModel):
    item_id: str
    kind: str
    track_index: int
    record_start: int
    record_end: int
    source_start: int
    source_end: int
    media_path: str
    linked_ids: tuple[str, ...]
    passed: bool
    detail: str = ""


class BuildOutput(StrictModel):
    package_artifact_id: str
    package_content_hash: str
    project_name: str
    timeline_name: str
    timeline_fingerprint: Sha256
    swept_projects: tuple[str, ...]
    items: tuple[ItemReadbackRow, ...]
    conformance: ConformanceTable
    render: RenderResult
    subtitle: SubtitleResult | None


def readback_fingerprint(rows: tuple[ItemReadbackRow, ...]) -> Sha256:
    """sha256 over the canonical observed-readback table (recomputable anywhere)."""

    ordered = sorted(
        rows,
        key=lambda row: (row.record_start, KIND_RANK.get(row.kind, 9), row.item_id),
    )
    lines = [
        "|".join(
            (
                row.item_id,
                row.kind,
                str(row.track_index),
                str(row.record_start),
                str(row.record_end),
                str(row.source_start),
                str(row.source_end),
                row.media_path,
                ",".join(sorted(row.linked_ids)),
            )
        )
        for row in ordered
    ]
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def as_frame(value: float, what: str) -> int:
    """Strict integer-frame coercion; fractional or non-numeric readback fails."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildFailure(
            "readback-mismatch", f"{what} returned a non-numeric value: {value!r}"
        )
    if isinstance(value, float):
        if not value.is_integer():
            raise BuildFailure(
                "readback-mismatch", f"{what} returned a fractional frame: {value!r}"
            )
        return int(value)
    return int(value)
