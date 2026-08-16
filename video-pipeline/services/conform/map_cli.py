"""QA CLI for Conform Maps (todo-23).

``build`` runs the frozen chain (fixture -> ingest manifest -> normalize ->
probe facts -> map), validates the result semantically, and commits it
atomically; ``--replay`` rebuilds and asserts identical canonical bytes.
``validate`` re-checks a committed map against its manifest/record.
``query`` exercises the conversion API. ``fault`` runs the offline crafted
corruption scenarios (``services.conform.map_faults``). Failures print
``error=<label>``/``reason=<code>`` and exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from services.conform.coordinates import OriginalTimestamp, PtsSpan, RationalTimeBase
from services.conform.errors import CoordinateError
from services.conform.map_build import ConformMapBuildError, build_conform_map
from services.conform.map_faults import run_scenario
from services.conform.map_models import ConformMap
from services.conform.map_probe import probe_map_facts
from services.conform.map_query import (
    edit_frame_to_original_pts,
    edit_sample_to_original_sample,
    original_pts_to_edit_frame,
    original_sample_to_edit_sample,
    original_span_to_edit_span,
)
from services.conform.map_validate import MapValidationError, validate_conform_map
from services.contracts.serialization import canonical_json_bytes
from services.foundation_io import atomic_write
from services.ingest.models import SourceManifest
from services.normalize.cli_support import build_source_manifest
from services.normalize.errors import NormalizeError
from services.normalize.models import NormalizeRecord
from services.normalize.runner import NormalizeContext, normalize_one


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="services.conform.map_cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--source-manifest", type=Path)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--fixture-id", required=True)
    build.add_argument("--lock", type=Path, required=True)
    build.add_argument("--ffmpeg", type=Path, required=True)
    build.add_argument("--ffprobe", type=Path, required=True)
    build.add_argument("--media-dir", type=Path, required=True)
    build.add_argument("--work-dir", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--audio-content-offset", type=int, default=0)
    build.add_argument("--replay", action="store_true")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--map", type=Path, required=True)
    validate.add_argument("--source-manifest", type=Path, required=True)
    validate.add_argument("--normalize-record", type=Path, required=True)

    query = subparsers.add_parser("query")
    query.add_argument("--map", type=Path, required=True)
    query.add_argument("--mode", required=True)
    query.add_argument("--frame", type=int)
    query.add_argument("--pts", type=int)
    query.add_argument("--start-pts", type=int)
    query.add_argument("--end-pts", type=int)
    query.add_argument("--sample", type=int)
    query.add_argument("--time-base-num", type=int)
    query.add_argument("--time-base-den", type=int)

    fault = subparsers.add_parser("fault")
    fault.add_argument("--scenario", required=True)
    fault.add_argument("--write", type=Path)
    return parser


def _cmd_build(arguments: argparse.Namespace) -> int:
    manifest = build_source_manifest(arguments)
    record_out = (
        arguments.work_dir / "records" / f"{arguments.fixture_id}.normalize-record.json"
    )
    record = normalize_one(
        manifest,
        "cfr30",
        NormalizeContext(
            lock_path=arguments.lock,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            output_dir=arguments.work_dir / "edit-sources",
            record_out=record_out,
        ),
    )
    facts = probe_map_facts(
        arguments.ffprobe, manifest=manifest, record=record
    )
    conform_map = build_conform_map(
        manifest,
        record,
        facts,
        audio_content_offset_samples=arguments.audio_content_offset,
    )
    validate_conform_map(conform_map, source_manifest=manifest, normalize_record=record)
    payload = canonical_json_bytes(conform_map)
    if arguments.replay:
        replayed = build_conform_map(
            manifest,
            record,
            facts,
            audio_content_offset_samples=arguments.audio_content_offset,
        )
        if canonical_json_bytes(replayed) != payload:
            print("error=replay_nondeterministic: rebuild produced different bytes")
            return 2
    atomic_write(arguments.out, payload)
    print(
        json.dumps(
            {
                "artifact_id": conform_map.artifact_id,
                "audio_content_offset_samples": (
                    conform_map.normalization.audio_content_offset_samples
                ),
                "dropped": len(conform_map.normalization.dropped_source_frames),
                "duplicated": len(conform_map.normalization.duplicated_source_frames),
                "output_frames": conform_map.video_table.output_frames,
                "table_sha256": conform_map.table_sha256,
            },
            sort_keys=True,
        )
    )
    print("conform-map: committed")
    if arguments.replay:
        print("replay: identical")
    print(f"map={arguments.out}")
    return 0


def _cmd_validate(arguments: argparse.Namespace) -> int:
    conform_map = ConformMap.model_validate_json(arguments.map.read_bytes())
    manifest = SourceManifest.model_validate_json(
        arguments.source_manifest.read_bytes()
    )
    record = NormalizeRecord.model_validate_json(
        arguments.normalize_record.read_bytes()
    )
    validate_conform_map(conform_map, source_manifest=manifest, normalize_record=record)
    print("conform-map: valid")
    return 0


def _time_base(arguments: argparse.Namespace) -> RationalTimeBase:
    if arguments.time_base_num is None or arguments.time_base_den is None:
        raise ValueError("query mode needs --time-base-num/--time-base-den")
    return RationalTimeBase(num=arguments.time_base_num, den=arguments.time_base_den)


def _cmd_query(arguments: argparse.Namespace) -> int:
    conform_map = ConformMap.model_validate_json(arguments.map.read_bytes())
    validate_conform_map(conform_map)
    if arguments.mode == "frame-to-pts":
        if arguments.frame is None:
            raise ValueError("frame-to-pts needs --frame")
        timestamp = edit_frame_to_original_pts(conform_map, arguments.frame)
        result = {"pts": timestamp.pts, "time_base_num": timestamp.time_base.num}
    elif arguments.mode == "pts-to-frame":
        if arguments.pts is None:
            raise ValueError("pts-to-frame needs --pts")
        frame = original_pts_to_edit_frame(
            conform_map,
            OriginalTimestamp(pts=arguments.pts, time_base=_time_base(arguments)),
        )
        result = {"edit_frame": frame}
    elif arguments.mode == "span":
        if arguments.start_pts is None or arguments.end_pts is None:
            raise ValueError("span needs --start-pts/--end-pts")
        span = original_span_to_edit_span(
            conform_map,
            PtsSpan(
                start_pts=arguments.start_pts,
                end_pts=arguments.end_pts,
                time_base=_time_base(arguments),
            ),
        )
        result = {"start_frame": span.start_frame, "end_frame": span.end_frame}
    elif arguments.mode == "audio-forward":
        if arguments.sample is None:
            raise ValueError("audio-forward needs --sample")
        result = {
            "edit_sample": original_sample_to_edit_sample(conform_map, arguments.sample)
        }
    else:
        if arguments.sample is None:
            raise ValueError("audio-reverse needs --sample")
        result = {
            "original_sample": edit_sample_to_original_sample(
                conform_map, arguments.sample
            )
        }
    print(json.dumps(result, sort_keys=True))
    print("conform-query: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "fault":
            return run_scenario(arguments.scenario, arguments.write)
        if arguments.command == "build":
            return _cmd_build(arguments)
        if arguments.command == "validate":
            return _cmd_validate(arguments)
        return _cmd_query(arguments)
    except MapValidationError as error:
        print(f"reason={error.reason_code}")
        print(f"error=map_invalid: {error}", file=sys.stderr)
    except ConformMapBuildError as error:
        print(f"reason={error.reason_code}")
        print(f"error=map_build_failed: {error}", file=sys.stderr)
    except CoordinateError as error:
        print("reason=coordinate_range")
        print(f"error={error.LABEL}: {error}", file=sys.stderr)
    except NormalizeError as error:
        print(f"error={error.label}: {error}", file=sys.stderr)
    except ValidationError as error:
        print("reason=malformed_map")
        print(f"error=malformed_map: {error.errors()[0]['type']}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"error=map_cli_failed: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
