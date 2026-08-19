"""Black/freeze span probing over a final render (pinned tools only).

Primary path: the pinned build's ``blackdetect``/``freezedetect`` filters,
used only after a live ``-filters`` check proves they exist. Fallback path:
the Todo-35 raw-luma decode (bounded gray-frame extraction) with black runs
measured by the frozen Todo-35 classifier and freeze runs measured as
consecutive decoded frames whose mean-abs diff is exactly zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from services.analyze.audio_probe import PinnedAudioTools
from services.analyze.visual_decode import bind_decode, probe_video_facts
from services.analyze.visual_metrics import compute_frame_facts, is_black
from services.foundation_io import sha256_file
from services.qc.tools import QcToolError, QcTools

_BLACK_LINE: Final = re.compile(
    r"black_start:(?P<start>\d+(?:\.\d+)?)\s+black_end:(?P<end>\d+(?:\.\d+)?)"
    r"\s+black_duration:(?P<duration>\d+(?:\.\d+)?)"
)
_FREEZE_EVENT: Final = re.compile(
    r"lavfi\.freezedetect\.(freeze_start|freeze_duration|freeze_end):\s*(\d+(?:\.\d+)?)"
)
_MIN_FILTER_TOKENS: Final = 2


@dataclass(frozen=True, slots=True)
class SpanMs:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class BlackFreezeProbe(Protocol):
    def black(self, render: Path, min_duration_ms: int, video_duration_ms: int) -> tuple[
        SpanMs, ...
    ]: ...

    def freeze(self, render: Path, min_duration_ms: int, video_duration_ms: int) -> tuple[
        SpanMs, ...
    ]: ...


def _runs(frame_count: int, flagged: tuple[bool, ...]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(flagged):
        if value and start is None:
            start = index
        elif not value and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, frame_count))
    return spans


def _raw_luma_flagged(
    tools: QcTools, render: Path, *, black: bool
) -> tuple[tuple[bool, ...], int, int]:
    """Decode gray frames (Todo-35 path); returns per-frame flags plus rate."""

    facts = probe_video_facts(tools.ffprobe, render)
    pinned = PinnedAudioTools(
        ffmpeg=tools.ffmpeg,
        ffprobe=tools.ffprobe,
        ffmpeg_sha256=tools.ffmpeg_sha256,
        ffprobe_sha256=tools.ffprobe_sha256,
    )
    _binding, frames = bind_decode(render, sha256_file(render), facts, tools=pinned)
    computed = compute_frame_facts(frames)
    if black:
        flagged = tuple(is_black(fact) for fact in computed)
    else:
        flagged = tuple(
            fact.frame_index > 0 and fact.diff_prev_m == 0 for fact in computed
        )
    return flagged, facts.rate_num, facts.rate_den


class PinnedBlackFreezeProbe:
    """Filter-first probe with the Todo-35 raw-luma fallback."""

    def __init__(self, tools: QcTools) -> None:
        self._tools = tools
        self._filters: frozenset[str] | None = None

    def _available(self) -> frozenset[str]:
        if self._filters is None:
            result = self._tools.run(
                (str(self._tools.ffmpeg), "-nostdin", "-hide_banner", "-filters"),
                120,
                "ffmpeg filters probe",
            )
            if result.returncode != 0:
                raise QcToolError("ffmpeg -filters probe failed")
            names = {
                line.split()[1]
                for line in result.stdout.splitlines()
                if len(line.split()) >= _MIN_FILTER_TOKENS
                and set(line.split()[0]) <= {"T", "S", "C", ".", "|"}
            }
            self._filters = frozenset(names)
        return self._filters

    def _filter_spans(
        self, render: Path, filterarg: str, pattern: re.Pattern[str], what: str
    ) -> tuple[SpanMs, ...]:
        result = self._tools.run(
            (
                str(self._tools.ffmpeg),
                "-nostdin",
                "-v",
                "info",
                "-i",
                str(render),
                "-vf",
                filterarg,
                "-an",
                "-f",
                "null",
                "-",
            ),
            300,
            what,
        )
        if result.returncode != 0:
            raise QcToolError(f"{what} failed: {result.stderr.strip()[-300:]}")
        return tuple(
            SpanMs(
                start_ms=int(float(match.group("start")) * 1000),
                end_ms=int(float(match.group("end")) * 1000),
            )
            for match in pattern.finditer(result.stderr)
        )

    def _black(self, render: Path, min_duration_ms: int) -> tuple[SpanMs, ...]:
        if "blackdetect" in self._available():
            return self._filter_spans(
                render,
                f"blackdetect=d={min_duration_ms / 1000}:pix_th=0.10",
                _BLACK_LINE,
                "blackdetect probe",
            )
        flagged, rate_num, rate_den = _raw_luma_flagged(self._tools, render, black=True)
        return tuple(
            SpanMs(
                start_ms=start * 1000 * rate_den // rate_num,
                end_ms=end * 1000 * rate_den // rate_num,
            )
            for start, end in _runs(len(flagged), flagged)
            if (end - start) * 1000 * rate_den // rate_num >= min_duration_ms
        )

    def _freeze(
        self, render: Path, min_duration_ms: int, video_duration_ms: int
    ) -> tuple[SpanMs, ...]:
        if "freezedetect" in self._available():
            result = self._tools.run(
                (
                    str(self._tools.ffmpeg),
                    "-nostdin",
                    "-v",
                    "info",
                    "-i",
                    str(render),
                    "-vf",
                    f"freezedetect=n=0.001:d={min_duration_ms / 1000}",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ),
                300,
                "freezedetect probe",
            )
            if result.returncode != 0:
                raise QcToolError(f"freezedetect probe failed: {result.stderr.strip()[-300:]}")
            spans: list[SpanMs] = []
            pending_start: int | None = None
            last_duration: int | None = None
            for match in _FREEZE_EVENT.finditer(result.stderr):
                event, value = match.group(1), int(float(match.group(2)) * 1000)
                if event == "freeze_start":
                    pending_start = value
                    last_duration = None
                elif event == "freeze_duration":
                    last_duration = value
                elif event == "freeze_end" and pending_start is not None:
                    spans.append(SpanMs(start_ms=pending_start, end_ms=value))
                    pending_start = None
            if pending_start is not None:
                end = (
                    video_duration_ms
                    if last_duration is None
                    else pending_start + last_duration
                )
                spans.append(SpanMs(start_ms=pending_start, end_ms=end))
            return tuple(spans)
        flagged, rate_num, rate_den = _raw_luma_flagged(self._tools, render, black=False)
        return tuple(
            SpanMs(
                start_ms=start * 1000 * rate_den // rate_num,
                end_ms=end * 1000 * rate_den // rate_num,
            )
            for start, end in _runs(len(flagged), flagged)
            if (end - start) * 1000 * rate_den // rate_num >= min_duration_ms
        )

    def black(
        self, render: Path, min_duration_ms: int, video_duration_ms: int
    ) -> tuple[SpanMs, ...]:
        del video_duration_ms
        return self._black(render, min_duration_ms)

    def freeze(
        self, render: Path, min_duration_ms: int, video_duration_ms: int
    ) -> tuple[SpanMs, ...]:
        return self._freeze(render, min_duration_ms, video_duration_ms)


__all__ = ["BlackFreezeProbe", "PinnedBlackFreezeProbe", "SpanMs"]
