"""QA CLI for normalize runs (mirrors ``services.ingest.cli``).

``run`` either consumes a registered Source Manifest or materializes +
registers a frozen Phase-0B fixture inline, then executes ``normalize_one``.
Success prints ``normalize: committed``; refusals print
``error=<label>`` / ``reason=<code>`` and exit 2. ``QA_FAULT_FIXTURE``
switches to the offline adversarial scenarios in ``services.normalize.cli_faults``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from services.ingest.fixture_inputs import FixtureMaterializationError
from services.normalize.cli_faults import FaultRun, FaultSpecError, load_fault_spec
from services.normalize.cli_support import build_source_manifest
from services.normalize.errors import NormalizeError, NormalizeVerificationError
from services.normalize.runner import NormalizeContext, normalize_one

DEFAULT_TARGET = "cfr30"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="services.normalize.cli")
    run = parser.add_subparsers(dest="command", required=True).add_parser("run")
    run.add_argument("--source-manifest", type=Path)
    run.add_argument("--manifest", type=Path)
    run.add_argument("--fixture-id")
    run.add_argument("--lock", type=Path, required=True)
    run.add_argument("--ffmpeg", type=Path, required=True)
    run.add_argument("--ffprobe", type=Path, required=True)
    run.add_argument("--target", default=DEFAULT_TARGET)
    run.add_argument("--media-dir", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    return parser


def _run(arguments: argparse.Namespace) -> int:
    manifest = build_source_manifest(arguments)
    record = normalize_one(
        manifest,
        arguments.target,
        NormalizeContext(
            lock_path=arguments.lock,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            output_dir=arguments.output_dir,
            record_out=arguments.out,
        ),
    )
    print(
        json.dumps(
            {
                "artifact_id": record.artifact_id,
                "output_frames": record.drop_dup.expected.output_frames,
                "output_sha256": record.output.sha256,
                "source_sha256": record.source.sha256,
            },
            sort_keys=True,
        )
    )
    print("normalize: committed")
    print(f"record={arguments.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if (fault_fixture := os.environ.get("QA_FAULT_FIXTURE")) is not None:
        try:
            return FaultRun(load_fault_spec(Path(fault_fixture)), arguments).run()
        except FaultSpecError as error:
            print(f"error=fault_spec: {error}", file=sys.stderr)
            return 2
    try:
        return _run(arguments)
    except NormalizeVerificationError as error:
        print(f"reason={error.reason_code}", file=sys.stderr)
        print(f"error=output_verification_failed: {error}", file=sys.stderr)
    except NormalizeError as error:
        print(f"error={error.label}: {error}", file=sys.stderr)
    except (FixtureMaterializationError, ValueError, OSError) as error:
        print(f"error=normalize_error: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
