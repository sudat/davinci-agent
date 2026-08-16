"""Verdict analysis: timestamp monotonicity, VFR evidence, eligibility."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Final

from services.conform.coordinates import OriginalTimestamp, require_monotonic_pts
from services.conform.errors import NonMonotonicPtsError
from services.ingest.models import (
    DeltaClass,
    Eligibility,
    EligibilityReason,
    StreamMonotonicity,
    VfrEvidence,
    VideoStreamRecord,
)
from services.ingest.probe import PacketTimestamps, stream_entries
from services.ingest.records import _text, hdr_signaling

UNSUPPORTED_TRANSFERS: Final = frozenset({"smpte2084", "arib-std-b67"})


def monotonicity_report(
    stream_index: int, samples: Sequence[OriginalTimestamp]
) -> StreamMonotonicity:
    try:
        require_monotonic_pts(samples)
        return StreamMonotonicity(
            stream_index=stream_index,
            monotonic=True,
            first_violation_index=None,
            sampled_packets=len(samples),
        )
    except NonMonotonicPtsError:
        violation = 0
        for index in range(1, len(samples)):
            if samples[index].seconds < samples[index - 1].seconds:
                violation = index
                break
        return StreamMonotonicity(
            stream_index=stream_index,
            monotonic=False,
            first_violation_index=violation,
            sampled_packets=len(samples),
        )


def vfr_evidence(
    stream: VideoStreamRecord, samples: Sequence[PacketTimestamps]
) -> VfrEvidence:
    ticks = sorted(sample.pts for sample in samples if sample.pts is not None)
    counts: dict[int, int] = {}
    for previous, current in itertools.pairwise(ticks):
        delta = current - previous
        counts[delta] = counts.get(delta, 0) + 1
    classes = tuple(
        DeltaClass(delta_ticks=delta, count=count) for delta, count in sorted(counts.items())
    )
    return VfrEvidence(
        is_vfr=len(classes) > 1,
        delta_classes=classes,
        time_base_num=stream.time_base_num,
        time_base_den=stream.time_base_den,
        sampled_packets=len(samples),
        basis="packet-pts-deltas-v1",
    )


def _non_monotonic_reasons(
    monotonic: Sequence[StreamMonotonicity],
) -> list[EligibilityReason]:
    return [
        EligibilityReason(
            code="non_monotonic",
            detail=(
                f"stream {entry.stream_index} pts decreases at sampled packet "
                f"{entry.first_violation_index}"
            ),
        )
        for entry in monotonic
        if not entry.monotonic
    ]


def _hdr_reasons(streams: Sequence[dict[str, object]]) -> list[EligibilityReason]:
    reasons: list[EligibilityReason] = []
    for stream in streams:
        if _text(stream, "codec_type") != "video":
            continue
        hdr = hdr_signaling(stream)
        if not (
            hdr.dolby_vision_rpu
            or hdr.hdr10_mastering_display
            or hdr.smpte2094
            or (hdr.color_transfer in UNSUPPORTED_TRANSFERS)
        ):
            continue
        detail = "HDR signaling requires an explicit color management recipe"
        if hdr.dolby_vision_rpu:
            detail += f" (dolby_vision_rpu profile={hdr.dolby_vision_profile})"
        reasons.append(EligibilityReason(code="unsupported_hdr", detail=detail))
    return reasons


def evaluate_eligibility(
    *,
    streams_payload: dict[str, object],
    monotonic: Sequence[StreamMonotonicity],
    extra_reasons: Sequence[EligibilityReason],
) -> Eligibility:
    streams = stream_entries(streams_payload)
    reasons = list(extra_reasons)
    codec_types = {_text(stream, "codec_type") for stream in streams}
    if "video" not in codec_types or "audio" not in codec_types:
        missing = "audio" if "audio" not in codec_types else "video"
        reasons.append(
            EligibilityReason(code="missing_stream", detail=f"no {missing} stream in container")
        )
    reasons.extend(_hdr_reasons(streams))
    reasons.extend(_non_monotonic_reasons(monotonic))
    if reasons:
        return Eligibility(verdict="blocked", reasons=tuple(reasons))
    return Eligibility(verdict="supported", reasons=())
