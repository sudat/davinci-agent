"""QA CLI for ingest registration (probe-style, like ``services.conform.cli``).

``register-fixture`` materializes a frozen Phase-0B variant (if the pinned
recipe report does not already bind the output) and registers it; success
prints ``register: supported``, blocked inputs print
``verdict=blocked reason=<codes>`` and exit 2. ``QA_FAULT_FIXTURE`` switches
to the offline adversarial scenarios in ``services.ingest.cli_faults``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from services.ingest.cli_faults import FaultRun, FaultSpecError, load_fault_spec
from services.ingest.fixture_inputs import FixtureMaterializationError, materialize_fixture
from services.ingest.ingest import (
    IngestError,
    recipe_pointer,
    register_one,
)
from services.ingest.models import AudioStreamRecord, SourceManifest, VideoStreamRecord
from services.ingest.probe import ProbeError, ProbeToolDriftError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="services.ingest.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_fixture = subparsers.add_parser("register-fixture")
    register_fixture.add_argument("--manifest", type=Path, required=True)
    register_fixture.add_argument("--fixture-id", required=True)
    register_fixture.add_argument("--ffmpeg", type=Path, required=True)
    register_fixture.add_argument("--ffprobe", type=Path, required=True)
    register_fixture.add_argument("--pin", type=Path, required=True)
    register_fixture.add_argument("--out", type=Path, required=True)
    register_fixture.add_argument("--media-dir", type=Path)
    return parser


def _summary(manifest: SourceManifest) -> str:
    video = next(
        (stream for stream in manifest.streams if isinstance(stream, VideoStreamRecord)),
        None,
    )
    audio = next(
        (stream for stream in manifest.streams if isinstance(stream, AudioStreamRecord)),
        None,
    )
    payload = {
        "artifact_id": manifest.artifact_id,
        "container": manifest.container.format_name,
        "file_sha256": manifest.file.sha256,
        "is_vfr": manifest.vfr_evidence.is_vfr if manifest.vfr_evidence else None,
        "rotation_degrees": video.rotation_degrees if video else None,
        "sample_rate": audio.sample_rate if audio else None,
        "streams": len(manifest.streams),
        "verdict": manifest.eligibility.verdict,
    }
    return json.dumps(payload, sort_keys=True)


def _register_fixture(arguments: argparse.Namespace) -> int:
    media_dir = arguments.media_dir or arguments.out.parent / "fixtures"
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        return FaultRun(
            load_fault_spec(Path(fault_fixture)),
            manifest_path=arguments.manifest,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            out=arguments.out,
            media_dir=media_dir,
            pin=arguments.pin,
        ).run()
    media = materialize_fixture(arguments.manifest, arguments.ffmpeg, media_dir)
    manifest = register_one(
        original=media,
        ffprobe=arguments.ffprobe,
        recipe=recipe_pointer(arguments.pin, arguments.fixture_id),
        out=arguments.out,
    )
    print(_summary(manifest))
    codes = ",".join(reason.code for reason in manifest.eligibility.reasons)
    if manifest.eligibility.verdict == "supported":
        print("register: supported")
        print(f"manifest={arguments.out}")
        return 0
    print(f"verdict=blocked reason={codes}")
    print(f"manifest={arguments.out}")
    return 2


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return _register_fixture(arguments)
    except ProbeToolDriftError as error:
        print(f"error=ffprobe_hash_drift: {error}", file=sys.stderr)
    except ProbeError as error:
        print(f"error=probe_error: {error}", file=sys.stderr)
    except (FaultSpecError, FixtureMaterializationError, IngestError, LookupError) as error:
        print(f"error=ingest_error: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
