from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.contracts.primitives import Sha256

Sha256Value = Sha256


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FileEvidence(StrictModel):
    path: str = Field(min_length=1)
    sha256: Sha256Value


class HostIdentity(StrictModel):
    macos_version: str
    macos_build: str
    architecture: str


class EditionEvidence(StrictModel):
    value: Literal["studio", "free", "unverified"]
    evidence: tuple[str, ...]
    needs_live_verification: bool


class ResolveApplication(StrictModel):
    present: bool
    app_path: str
    info_plist: FileEvidence
    bundle_id: str
    version: str
    build: str
    edition: EditionEvidence


class ResolveDocs(StrictModel):
    app_bundle_paths: tuple[FileEvidence, ...]
    installed_paths: tuple[FileEvidence, ...]
    location_note: str


class ResolveBridge(StrictModel):
    library: FileEvidence
    module: FileEvidence


class ScriptingPosture(StrictModel):
    preference_paths: tuple[FileEvidence, ...]
    remote_access: Literal["disabled", "loopback", "network", "unknown"]
    runtime_policy: Literal["loopback-only", "network"]
    network_access_performed: bool
    needs_live_verification: bool
    reason: str


class GpuInventory(StrictModel):
    backend: Literal["metal", "unknown"]
    inventory: str


class AssetInventory(StrictModel):
    font_paths: tuple[str, ...]
    lut_paths: tuple[str, ...]
    fairlight_paths: tuple[str, ...]
    plugin_paths: tuple[str, ...]
    preset_paths: tuple[str, ...]


class OutputPosture(StrictModel):
    cwd: str
    writable: bool


class ResolveHostReport(StrictModel):
    schema_version: Literal["resolve-host-report-v1"] = "resolve-host-report-v1"
    host: HostIdentity
    application: ResolveApplication
    docs: ResolveDocs
    bridge: ResolveBridge
    scripting: ScriptingPosture
    gpu: GpuInventory
    assets: AssetInventory
    output: OutputPosture
