"""Offline fault scenarios for the fixed-presentation spike (QA_FAULT_FIXTURE mode).

Fault specs drive the real orchestration (build, ladder walk, render verify,
evidence) against in-memory fakes so text-encoding failure, unsupported
subtitle track placement, audio preset mismatch, and an exhausted fallback
ladder all surface as explicit partial/unsupported outcomes, never silent
success. Exit code is ``EXIT_FAULT`` whether or not the injected fault was
detected; the printed observation discriminates.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.contracts.primitives import StrictModel
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut_faults import FakeBaseCutResolve
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.connection import ResolveConnection, VersionBinding
from services.resolve_bridge.fixed_presentation import run_fixed_presentation
from services.resolve_bridge.fixed_presentation_evidence import stdout_lines
from services.resolve_bridge.fixed_presentation_fakes import FakeFpManager, FakeTools
from services.resolve_bridge.fixed_presentation_models import MARKER, SUBTITLE_SRT
from services.resolve_bridge.fixed_presentation_srt import (
    SubtitleTextError,
    cue_from_recipe,
    render_srt,
)
from services.resolve_bridge.fixed_presentation_tools import RenderError
from services.resolve_bridge.lifecycle import PROJECT_PREFIX

EXIT_FAULT: Final = 2
BAD_SRT_BYTES: Final = b"\xff\xfe\x00b\xa0a?d subtitle\n"

FAULT_KINDS: Final = {
    "text_encoding": "text-encoding-unsupported",
    "unsupported_track_placement": "placement=in-timeline:false",
    "preset_mismatch": "audio-preset-mismatch",
    "missing_fallback": "subtitle-placement-unsupported",
}


class FixedPresentationFaultSpec(StrictModel):
    fault: str

    @property
    def observation(self) -> str:
        return FAULT_KINDS[self.fault]


def _srt_path(
    fault: str, fixture_dir: Path, scratch: Path, manifest: Phase0AFixtureManifest
) -> Path:
    if fault == "text_encoding":
        bad = scratch / "subtitle.srt"
        bad.write_bytes(BAD_SRT_BYTES)
        return bad
    fixture_srt = fixture_dir / SUBTITLE_SRT
    if fixture_srt.is_file():
        return fixture_srt
    synthetic = scratch / "subtitle.srt"
    recipe = manifest.recipe
    cue = cue_from_recipe(
        recipe.subtitle, recipe.source.frame_rate.num, recipe.source.frame_rate.den
    )
    synthetic.write_bytes(render_srt(cue))
    return synthetic


def run_fault_cli(spec_path: Path, manifest_path: Path, fixture_dir: Path) -> int:
    try:
        spec = FixedPresentationFaultSpec.model_validate_json(spec_path.read_bytes())
        observation = FAULT_KINDS.get(spec.fault)
        manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
        media = fixture_media_map(fixture_dir)
        request = request_from_ir(ir_from_manifest(manifest), media, require_files=False)
        expected = expected_from_manifest(manifest, media)
    except (OSError, ValidationError, BaseCutError, KeyError) as error:
        print(f"{MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault not in FAULT_KINDS:
        print(f"{MARKER} fault-fixture invalid: unknown fault {spec.fault!r}", file=sys.stderr)
        return EXIT_FAULT
    with tempfile.TemporaryDirectory(prefix="fp-fault-") as scratch_name:
        scratch = Path(scratch_name)
        srt_path = _srt_path(spec.fault, fixture_dir, scratch, manifest)
        try:
            srt_bytes = srt_path.read_bytes()
        except OSError as error:
            print(f"{MARKER} fault-fixture invalid srt: {error}", file=sys.stderr)
            return EXIT_FAULT
        tools = FakeTools(spec.fault, srt_bytes)
        manager = FakeFpManager(scratch / "render")
        connection = ResolveConnection(
            resolve=FakeBaseCutResolve(manager),
            binding=VersionBinding(
                product_name="DaVinci Resolve Studio",
                version_core="21.0.4",
                build_number=5,
                version_string="21.0.4.5",
            ),
        )
        try:
            report, render_summary = run_fixed_presentation(
                connection=connection,
                request=request,
                expected=expected,
                manifest=manifest,
                media=media,
                tools=tools,
                render_dir=scratch / "render",
                srt_path=srt_path,
                direct_allowed=True,
            )
            text = stdout_lines(report, render_summary)
        except (BaseCutError, RenderError, SubtitleTextError, OSError) as error:
            print(f"{MARKER} mismatch code=api-error {error}", file=sys.stderr)
            return _cleanup_exit(manager)
        print(text, end="")
        if observation is None or observation not in text:
            print(f"ERROR: fault {spec.fault!r} not observed as {observation!r}", file=sys.stderr)
        return _cleanup_exit(manager)


def _cleanup_exit(manager: FakeFpManager) -> int:
    leftover = [
        name for name in manager.GetProjectListInCurrentFolder() if name.startswith(PROJECT_PREFIX)
    ]
    if leftover:
        print(f"ERROR: owned projects leaked: {leftover}", file=sys.stderr)
    return EXIT_FAULT

