"""Render the literal Phase-0C Editorial Preview artifacts from the frozen 0A binding.

Produces ``preview.mp4`` and ``preview-trace.json`` in the requested output
directory (the acceptance target is ``$ATTEMPT_DIR/phase-0c``). Must run with
cwd fixed to ``$PIPELINE_ROOT`` so the frozen manifest and toolchain lock
resolve.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from services.fixtures.manifest import Phase0AFixtureManifest
from services.preview.binding import initial_bindings, initial_timeline_ir
from services.preview.render import PREVIEW_NAME, TRACE_NAME, render_preview
from services.preview.tools import load_pinned_tools

DEFAULT_MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="services.preview.cli")
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    manifest = Phase0AFixtureManifest.model_validate_json(arguments.manifest.read_bytes())
    ir = initial_timeline_ir(manifest)
    bindings = initial_bindings(arguments.fixture_dir)
    tools = load_pinned_tools()
    trace = render_preview(None, ir, bindings, arguments.out_dir, tools=tools)
    total = trace.timeline_binding.total_record_frames
    out_dir = arguments.out_dir.resolve()
    print(f"preview: complete path={out_dir / PREVIEW_NAME} frames={total}")
    print(f"preview-trace: path={out_dir / TRACE_NAME} coverage=[0,{total})")
    print(f"decoded_video_sha256={trace.preview.decoded_video_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
