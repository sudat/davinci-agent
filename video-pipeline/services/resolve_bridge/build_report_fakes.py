"""Offline fakes for the Build-Report 0A spike fault harness.

Reuses the fixed-presentation fakes (disposable project, render job, media
tools) so the real writer and verifier run offline against an injected fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.build_report import BuildReport0A
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import canonical_model_bytes
from services.resolve_bridge.base_cut_faults import FakeBaseCutResolve
from services.resolve_bridge.base_cut_plan import (
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.build_report import run_spike
from services.resolve_bridge.connection import ResolveConnection, VersionBinding
from services.resolve_bridge.fixed_presentation_fakes import FakeFpManager, FakeTools
from services.resolve_bridge.fixed_presentation_models import SUBTITLE_SRT
from services.resolve_bridge.fixed_presentation_srt import cue_from_recipe, render_srt
from services.resolve_bridge.fixed_presentation_tools import DecodeOutcome
from services.resolve_bridge.models import (
    AssetInventory,
    EditionEvidence,
    FileEvidence,
    GpuInventory,
    HostIdentity,
    OutputPosture,
    ResolveApplication,
    ResolveBridge,
    ResolveDocs,
    ResolveHostReport,
    ScriptingPosture,
)
from services.resolve_bridge.readiness import APP, SCRIPTING

if TYPE_CHECKING:
    from services.contracts.build_report import BuildReport0A

SYNTHETIC_SHA = "1" * 64
RATE_NUM = 30
RATE_DEN = 1


class FakeBuildTools(FakeTools):
    """FakeTools plus a switchable decode-pass failure for fault scenarios."""

    def __init__(self, *, decode_fails: bool = False) -> None:
        super().__init__("", b"1\n00:00:06,000 --> 00:00:08,000\nPHASE 0A FIXED SUBTITLE\n")
        self.decode_fails = decode_fails

    def decode(self, path: Path) -> DecodeOutcome:
        if self.decode_fails:
            return DecodeOutcome(
                argv=(f"ffmpeg({path.name})", "-f", "null"),
                exit_code=1,
                stderr_tail="simulated pinned-ffmpeg decode failure",
            )
        return super().decode(path)


def synthetic_host_report() -> ResolveHostReport:
    return ResolveHostReport(
        host=HostIdentity(macos_version="15.0", macos_build="24A335", architecture="arm64"),
        application=ResolveApplication(
            present=True,
            app_path=str(APP),
            info_plist=FileEvidence(path="/synthetic-info.plist", sha256=SYNTHETIC_SHA),
            bundle_id="com.blackmagic-design.DaVinciResolve",
            version="21.0.4",
            build="21.0.40005",
            edition=EditionEvidence(
                value="unverified",
                evidence=("synthetic build-report fault scenario",),
                needs_live_verification=True,
            ),
        ),
        docs=ResolveDocs(
            app_bundle_paths=(),
            installed_paths=(FileEvidence(path="/synthetic-readme.txt", sha256=SYNTHETIC_SHA),),
            location_note="synthetic build-report fault scenario",
        ),
        bridge=ResolveBridge(
            library=FileEvidence(
                path=str(APP / "Contents/Libraries/Fusion/fusionscript.so"), sha256=SYNTHETIC_SHA
            ),
            module=FileEvidence(
                path=str(SCRIPTING / "Modules/DaVinciResolveScript.py"), sha256=SYNTHETIC_SHA
            ),
        ),
        scripting=ScriptingPosture(
            preference_paths=(),
            remote_access="unknown",
            runtime_policy="loopback-only",
            network_access_performed=False,
            needs_live_verification=True,
            reason="synthetic build-report fault scenario",
        ),
        gpu=GpuInventory(backend="metal", inventory="synthetic"),
        assets=AssetInventory(
            font_paths=(), lut_paths=(), fairlight_paths=(), plugin_paths=(), preset_paths=()
        ),
        output=OutputPosture(cwd="/synthetic", writable=True),
    )


@dataclass(frozen=True, slots=True)
class FakeSpike:
    report: BuildReport0A
    render_path: Path
    manifest_path: Path
    host_report_path: Path
    ffmpeg_bin: Path
    ffprobe_bin: Path
    manager: FakeFpManager
    tools: FakeBuildTools


def run_fake_spike(
    manifest_path: Path,
    fixture_dir: Path,
    scratch: Path,
    *,
    pool_fault: str = "",
    decode_fails: bool = False,
) -> FakeSpike:
    manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
    media = fixture_media_map(fixture_dir)
    request = request_from_ir(ir_from_manifest(manifest), media, require_files=False)
    expected = expected_from_manifest(manifest, media)
    fixture_srt = fixture_dir / SUBTITLE_SRT
    if fixture_srt.is_file():
        srt_path = fixture_srt
    else:
        srt_path = scratch / SUBTITLE_SRT
        srt_path.write_bytes(
            render_srt(cue_from_recipe(manifest.recipe.subtitle, RATE_NUM, RATE_DEN))
        )
    ffmpeg_bin = scratch / "ffmpeg"
    ffmpeg_bin.write_bytes(b"fake-ffmpeg-binary")
    ffprobe_bin = scratch / "ffprobe"
    ffprobe_bin.write_bytes(b"fake-ffprobe-binary")
    tools = FakeBuildTools(decode_fails=decode_fails)
    manager = FakeFpManager(scratch / "render", fault=pool_fault)
    connection = ResolveConnection(
        resolve=FakeBaseCutResolve(manager),
        binding=VersionBinding(
            product_name="DaVinci Resolve Studio",
            version_core="21.0.4",
            build_number=5,
            version_string="21.0.4.5",
        ),
    )
    host_report_path = scratch / "resolve-host.json"
    host_report_path.write_bytes(canonical_model_bytes(synthetic_host_report()))
    report = run_spike(
        connection,
        request,
        expected,
        manifest,
        media,
        tools,
        scratch / "render",
        srt_path,
        host_report_path=host_report_path,
        manifest_path=manifest_path,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
    )
    return FakeSpike(
        report=report,
        render_path=Path(report.render_output.output_path),
        manifest_path=manifest_path,
        host_report_path=host_report_path,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        manager=manager,
        tools=tools,
    )
