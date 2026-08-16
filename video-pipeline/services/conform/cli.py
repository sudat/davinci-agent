"""QA probe CLI for coordinate arithmetic.

Success paths print a short observation; every failure path exits 2 with an
explicit ``error=<label>:`` line. Labels: ``coordinate_overflow``,
``non_monotonic_pts``, ``negative_span``, ``inverted_span``, ``malformed_input``,
``off_by_one``, ``drop_frame_boundary_mismatch``, ``coordinate_range``.
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from typing import Any

from pydantic import ValidationError

from services.conform.convert import (
    audio_sample_for_pts,
    frame_conversion_accounting,
    pts_floor_for_audio_sample,
    pts_floor_for_video_frame,
    record_span_for_source_span,
    video_frame_for_pts,
)
from services.conform.coordinates import OriginalTimestamp, RationalTimeBase, require_monotonic_pts
from services.conform.errors import CoordinateError, MalformedCoordinateError
from services.contracts.primitives import RationalFrameRate, SourceFrameSpan

FRAME_PROBES = (0, 1, 2, 3, 149, 150, 299, 300, 449, 450, 499, 500, 599, 600)
SAMPLE_PROBES = (0, 1, 2, 3, 7, 8, 15, 16, 47999, 48000, 96000)


def _parse_int(value: str, what: str) -> int:
    try:
        return int(value, 10)
    except ValueError as error:
        raise MalformedCoordinateError(f"{what} {value!r} is not a base-10 integer") from error


def _battery_video() -> int:
    frame_round_trips = 0
    for fps_num, fps_den in ((24, 1), (30000, 1001), (60000, 1001)):
        rate = RationalFrameRate(num=fps_num, den=fps_den)
        for tb_num, tb_den in ((1, 90000), (1, 1000), (1, 30000)):
            time_base = RationalTimeBase(num=tb_num, den=tb_den)
            for frame in FRAME_PROBES:
                timestamp = pts_floor_for_video_frame(frame, rate, time_base)
                exact = Fraction(frame) / (rate.as_fraction * time_base.as_fraction)
                if timestamp.pts != exact.numerator // exact.denominator:
                    raise ValueError(f"floor drift at frame {frame}")
                if video_frame_for_pts(timestamp, rate) == frame:
                    frame_round_trips += 1
    return frame_round_trips


def _battery_audio() -> tuple[int, int]:
    round_trips = 0
    off_lattice_documented = 0
    for sample_rate, tb_den in ((48000, 90000), (48000, 48000), (96000, 90000), (96000, 96000)):
        time_base = RationalTimeBase(num=1, den=tb_den)
        for sample in SAMPLE_PROBES:
            timestamp = pts_floor_for_audio_sample(sample, sample_rate, time_base)
            exact = Fraction(sample, sample_rate) / time_base.as_fraction
            if timestamp.pts != exact.numerator // exact.denominator:
                raise ValueError(f"floor drift at sample {sample}")
            if audio_sample_for_pts(timestamp, sample_rate) == sample:
                round_trips += 1
            else:
                off_lattice_documented += 1
    return round_trips, off_lattice_documented


def _cmd_cfr_vector(_arguments: argparse.Namespace) -> int:
    frame_round_trips = _battery_video()
    audio_round_trips, off_lattice_documented = _battery_audio()
    if off_lattice_documented == 0:
        raise ValueError("battery vacuous: expected at least one documented off-lattice case")
    summary = {
        "audio_round_trips": audio_round_trips,
        "audio_off_lattice_documented": off_lattice_documented,
        "frame_round_trips": frame_round_trips,
        "status": "ok",
    }
    print(json.dumps(summary, sort_keys=True))
    print("cfr exact vectors ok")
    return 0


def _cmd_convert_pts(arguments: argparse.Namespace) -> int:
    time_base = RationalTimeBase(num=arguments.time_base_num, den=arguments.time_base_den)
    timestamp = OriginalTimestamp(pts=arguments.pts, time_base=time_base)
    rate = RationalFrameRate(num=arguments.fps_num, den=arguments.fps_den)
    frame = video_frame_for_pts(timestamp, rate)
    print(json.dumps({"frame": frame, "pts": timestamp.pts}, sort_keys=True))
    print("pts conversion ok")
    return 0


def _cmd_pts_sequence(arguments: argparse.Namespace) -> int:
    time_base = RationalTimeBase(num=arguments.time_base_num, den=arguments.time_base_den)
    sequence = [
        OriginalTimestamp(pts=int(item), time_base=time_base)
        for item in arguments.pts.split(",")
        if item != ""
    ]
    require_monotonic_pts(sequence)
    print("pts sequence monotonic")
    return 0


def _span_from_arguments(arguments: argparse.Namespace) -> SourceFrameSpan:
    return SourceFrameSpan(
        start_frame=_parse_int(arguments.start_frame, "--start-frame"),
        end_frame=_parse_int(arguments.end_frame, "--end-frame"),
        rate=RationalFrameRate(num=arguments.fps_num, den=arguments.fps_den),
    )


def _cmd_convert_span(arguments: argparse.Namespace) -> int:
    span = _span_from_arguments(arguments)
    timeline_rate = RationalFrameRate(num=arguments.timeline_num, den=arguments.timeline_den)
    placed = record_span_for_source_span(span, arguments.base, timeline_rate)
    report = frame_conversion_accounting(span, timeline_rate)
    if arguments.expect_record_length is not None:
        expected = arguments.expect_record_length
        if placed.length != expected:
            print(
                f"error=off_by_one: record length computed={placed.length} "
                f"expected={expected}; drop/dup accounting: "
                f"dropped={list(report.dropped_source_frames)} "
                f"duplicated={list(report.duplicated_source_frames)}"
            )
            return 2
    payload = {
        "record_span": {"start_frame": placed.start_frame, "end_frame": placed.end_frame},
        "accounting": {
            "output_frames": report.output_frames,
            "dropped_source_frames": list(report.dropped_source_frames),
            "duplicated_source_frames": list(report.duplicated_source_frames),
        },
    }
    print(json.dumps(payload, sort_keys=True))
    print("record span ok")
    return 0


def _cmd_span_boundary(arguments: argparse.Namespace) -> int:
    rate = RationalFrameRate(num=arguments.fps_num, den=arguments.fps_den)
    target = RationalFrameRate(num=arguments.target_num, den=arguments.target_den)
    span = SourceFrameSpan(start_frame=0, end_frame=arguments.frames, rate=rate)
    report = frame_conversion_accounting(span, target)
    expected_duplicated = tuple(
        int(item) for item in arguments.expect_duplicated.split(",") if item != ""
    )
    if (
        report.output_frames != arguments.expect_output
        or report.duplicated_source_frames != expected_duplicated
    ):
        print(
            f"error=drop_frame_boundary_mismatch: output computed={report.output_frames} "
            f"expected={arguments.expect_output}; duplicated "
            f"computed={list(report.duplicated_source_frames)} "
            f"expected={list(expected_duplicated)}"
        )
        return 2
    print(
        json.dumps(
            {
                "output_frames": report.output_frames,
                "dropped_source_frames": list(report.dropped_source_frames),
                "duplicated_source_frames": list(report.duplicated_source_frames),
            },
            sort_keys=True,
        )
    )
    print("boundary ok")
    return 0


def _validation_label(error: ValidationError) -> str:
    kinds = {str(item["type"]) for item in error.errors()}
    if "coordinate_overflow" in kinds:
        return "coordinate_overflow"
    if "span_inverted" in kinds:
        return "inverted_span"
    if kinds & {"greater_than_equal", "greater_than"}:
        return "negative_span"
    return "malformed_input"


def _add_int_arguments(parser: argparse.ArgumentParser, names: tuple[str, ...]) -> None:
    for name in names:
        parser.add_argument(f"--{name}", type=int, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="services.conform.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cfr_vector = subparsers.add_parser("cfr-vector")
    cfr_vector.set_defaults(handler=_cmd_cfr_vector)

    convert_pts = subparsers.add_parser("convert-pts")
    _add_int_arguments(
        convert_pts, ("pts", "time-base-num", "time-base-den", "fps-num", "fps-den")
    )
    convert_pts.set_defaults(handler=_cmd_convert_pts)

    pts_sequence = subparsers.add_parser("pts-sequence")
    pts_sequence.add_argument("--pts", required=True)
    _add_int_arguments(pts_sequence, ("time-base-num", "time-base-den"))
    pts_sequence.set_defaults(handler=_cmd_pts_sequence)

    convert_span = subparsers.add_parser("convert-span")
    convert_span.add_argument("--start-frame", required=True)
    convert_span.add_argument("--end-frame", required=True)
    _add_int_arguments(
        convert_span, ("fps-num", "fps-den", "timeline-num", "timeline-den", "base")
    )
    convert_span.add_argument("--expect-record-length", type=int)
    convert_span.set_defaults(handler=_cmd_convert_span)

    span_boundary = subparsers.add_parser("span-boundary")
    _add_int_arguments(
        span_boundary,
        ("fps-num", "fps-den", "frames", "target-num", "target-den", "expect-output"),
    )
    span_boundary.add_argument("--expect-duplicated", required=True)
    span_boundary.set_defaults(handler=_cmd_span_boundary)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    handler: Any = arguments.handler
    try:
        return handler(arguments)
    except CoordinateError as error:
        print(f"error={error.LABEL}: {error}")
    except ValidationError as error:
        print(f"error={_validation_label(error)}: {error.errors()[0]['type']}")
    except ValueError as error:
        print(f"error=internal_check_failed: {error}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
