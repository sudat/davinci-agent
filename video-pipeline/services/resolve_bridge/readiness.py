from __future__ import annotations

import argparse
import os
import platform
import plistlib
import subprocess
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
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
from services.toolchain.models import update_host_binding

APP: Final = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
SUPPORT: Final = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve")
SCRIPTING: Final = SUPPORT / "Developer/Scripting"
PREFERENCES: Final = Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve"


class HostReadinessError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _evidence(path: Path) -> FileEvidence:
    resolved = path.resolve(strict=True)
    return FileEvidence(path=str(resolved), sha256=sha256_file(resolved))


def _command(argv: tuple[str, ...]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def _docs() -> ResolveDocs:
    app_candidates = (
        APP / "Contents/Developer/Scripting",
        APP / "Contents/Resources/Developer/Scripting",
    )
    app_files = tuple(
        _evidence(path)
        for root in app_candidates
        if root.is_dir()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    installed = (SCRIPTING / "README.txt", *sorted((SCRIPTING / "Examples").glob("*")))
    installed_files = tuple(_evidence(path) for path in installed if path.is_file())
    note = (
        "Developer/Scripting docs are inside the app bundle"
        if app_files
        else "installed support package; no Developer/Scripting docs found inside the app bundle"
    )
    return ResolveDocs(
        app_bundle_paths=app_files,
        installed_paths=installed_files,
        location_note=note,
    )


def _existing_paths(paths: tuple[Path, ...]) -> tuple[str, ...]:
    return tuple(str(path.resolve()) for path in paths if path.exists())


def _gpu() -> GpuInventory:
    output = _command(("system_profiler", "SPDisplaysDataType"))
    selected = tuple(
        line.strip()
        for line in output.splitlines()
        if any(
            marker in line
            for marker in ("Chipset Model:", "Total Number of Cores:", "Metal Support:")
        )
    )
    backend = "metal" if any("Metal Support:" in line for line in selected) else "unknown"
    return GpuInventory(backend=backend, inventory="; ".join(selected))


def probe_host() -> ResolveHostReport:
    plist_path = APP / "Contents/Info.plist"
    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    bundle_id = str(plist["CFBundleIdentifier"])
    version = str(plist["CFBundleShortVersionString"])
    build = str(plist["CFBundleVersion"])
    docs = _docs()
    readme_mentions_studio = any(
        "DaVinci Resolve Studio" in Path(item.path).read_text(errors="replace")
        for item in docs.installed_paths
        if Path(item.path).name == "README.txt"
    )
    edition_evidence = (
        f"bundle_id={bundle_id}",
        f"installed scripting README declares Studio={readme_mentions_studio}",
        "bundle identifier and installed files do not prove the licensed edition",
    )
    preference_candidates = (PREFERENCES / "config.dat", PREFERENCES / "config.user.xml")
    preference_evidence = tuple(
        _evidence(path) for path in preference_candidates if path.is_file()
    )
    return ResolveHostReport(
        host=HostIdentity(
            macos_version=_command(("sw_vers", "-productVersion")),
            macos_build=_command(("sw_vers", "-buildVersion")),
            architecture=platform.machine(),
        ),
        application=ResolveApplication(
            present=APP.is_dir(),
            app_path=str(APP),
            info_plist=_evidence(plist_path),
            bundle_id=bundle_id,
            version=version,
            build=build,
            edition=EditionEvidence(
                value="unverified",
                evidence=edition_evidence,
                needs_live_verification=True,
            ),
        ),
        docs=docs,
        bridge=ResolveBridge(
            library=_evidence(APP / "Contents/Libraries/Fusion/fusionscript.so"),
            module=_evidence(SCRIPTING / "Modules/DaVinciResolveScript.py"),
        ),
        scripting=ScriptingPosture(
            preference_paths=preference_evidence,
            remote_access="unknown",
            runtime_policy="loopback-only",
            network_access_performed=False,
            needs_live_verification=True,
            reason="installed preferences do not expose scripting scope as readable text",
        ),
        gpu=_gpu(),
        assets=AssetInventory(
            font_paths=_existing_paths((Path("/Library/Fonts"), Path.home() / "Library/Fonts")),
            lut_paths=_existing_paths(
                (
                    SUPPORT / "LUT",
                    Path.home()
                    / "Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
                )
            ),
            fairlight_paths=_existing_paths(
                (
                    SUPPORT / "Fairlight",
                    Path.home()
                    / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fairlight",
                )
            ),
            plugin_paths=_existing_paths(
                (Path("/Library/Audio/Plug-Ins"), Path.home() / "Library/Audio/Plug-Ins")
            ),
            preset_paths=_existing_paths((PREFERENCES / "Fairlight/Presets", PREFERENCES)),
        ),
        output=OutputPosture(
            cwd=str(Path.cwd().resolve()),
            writable=os.access(Path.cwd(), os.W_OK),
        ),
    )


def validate_host_report(report: ResolveHostReport) -> None:
    if (
        report.scripting.runtime_policy != "loopback-only"
        or report.scripting.remote_access == "network"
    ):
        raise HostReadinessError("unsafe Resolve scripting exposure")
    if report.scripting.network_access_performed:
        raise HostReadinessError("readiness probe performed network access")
    if not report.docs.installed_paths or not report.bridge.library.path:
        raise HostReadinessError("missing Resolve scripting provenance or docs")
    if not report.output.writable:
        raise HostReadinessError("pipeline cwd is not writable")


def load_host_report(path: Path) -> ResolveHostReport:
    try:
        report = ResolveHostReport.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise HostReadinessError(f"invalid Resolve host report: {error}") from error
    validate_host_report(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--update-lock", type=Path)
    parser.add_argument("--fixture", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        report = (
            ResolveHostReport.model_validate_json(arguments.fixture.read_bytes())
            if arguments.fixture is not None
            else probe_host()
        )
        atomic_write(arguments.out, canonical_model_bytes(report))
        validate_host_report(report)
        if arguments.update_lock is not None:
            update_host_binding(arguments.update_lock, arguments.out, report)
    except (HostReadinessError, OSError, subprocess.CalledProcessError, ValidationError) as error:
        print(error)
        return 2
    print(f"resolve-readonly ready: {report.application.version} build {report.application.build}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
