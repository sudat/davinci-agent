"""Shared value models, bindings, and constants for the Build-Report 0A spike."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from services.contracts.primitives import StrictModel
from services.resolve_bridge.readiness import load_host_report

if TYPE_CHECKING:
    from services.contracts.build_report import RenderProbeSummary0A
    from services.resolve_bridge.connection import VersionBinding
    from services.resolve_bridge.fixed_presentation_models import FfprobeReport
    from services.resolve_bridge.fixed_presentation_tools import DecodeOutcome, MediaToolsApi

MARKER: Final = "build-report:"
REPORT_NAME: Final = "build-report.json"
ADAPTER_MODULE: Final = "services.resolve_bridge"
SCHEMA_VERSION: Final = "build-report-0a-v1"


class VerifyMismatch(StrictModel):
    code: str
    detail: str


class VerifyOutcome(StrictModel):
    passed: bool
    mismatches: tuple[VerifyMismatch, ...]

    def lines(self) -> tuple[str, ...]:
        return tuple(
            f"{MARKER} mismatch code={row.code} detail={row.detail}" for row in self.mismatches
        )


@dataclass(frozen=True, slots=True)
class BindingInputs:
    host_report_path: Path
    manifest_path: Path
    ffmpeg_bin: Path
    ffprobe_bin: Path
    resolve_product: str
    resolve_version: str
    resolve_build: str
    adapter_version: str
    host_report_sha256: str
    manifest_sha256: str
    ffmpeg_sha256: str
    ffprobe_sha256: str


def adapter_version() -> str:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


def binding_inputs(
    binding: VersionBinding,
    tools: MediaToolsApi,
    *,
    host_report_path: Path,
    manifest_path: Path,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    adapter_ver: str,
) -> BindingInputs:
    host = load_host_report(host_report_path)
    return BindingInputs(
        host_report_path=host_report_path,
        manifest_path=manifest_path,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        resolve_product=binding.product_name,
        resolve_version=host.application.version,
        resolve_build=host.application.build,
        adapter_version=adapter_ver,
        host_report_sha256=tools.sha256(host_report_path),
        manifest_sha256=tools.sha256(manifest_path),
        ffmpeg_sha256=tools.sha256(ffmpeg_bin),
        ffprobe_sha256=tools.sha256(ffprobe_bin),
    )


def probe_matches(recorded: RenderProbeSummary0A, fresh: FfprobeReport) -> bool:
    """Field-wise equality of a recorded probe summary against a fresh probe.

    ``RenderStreamSummary0A`` mirrors the ``FfprobeStream`` field set exactly,
    so canonical model dumps compare directly.
    """

    if len(recorded.streams) != len(fresh.streams):
        return False
    for left, right in zip(recorded.streams, fresh.streams, strict=True):
        if left.model_dump() != right.model_dump():
            return False
    return recorded.format_name == fresh.format.format_name and (
        recorded.format_duration == fresh.format.duration
    )


CODE_CONTENT_HASH = "content-hash-mismatch"
CODE_ITEM_COUNT = "item-count-mismatch"
CODE_ITEM_REQUESTED = "item-requested-mismatch"
CODE_ITEM_OBSERVED = "item-observed-mismatch"
CODE_FINGERPRINT = "timeline-fingerprint-mismatch"
CODE_RENDER_JOB = "render-job-incomplete"
CODE_RENDER_OUTPUT = "render-output-missing"
CODE_RENDER_HASH = "render-hash-mismatch"
CODE_RENDER_PROBE = "render-probe-stale"
CODE_RENDER_DECODE = "render-decode-failed"
CODE_BINDING_HOST = "binding-host-report-hash-mismatch"
CODE_BINDING_MANIFEST = "binding-manifest-hash-mismatch"
CODE_BINDING_RESOLVE = "binding-resolve-version-mismatch"
CODE_BINDING_TOOL = "binding-tool-hash-mismatch"
CODE_FAILURES = "report-failures-present"
COMPLETE_PERCENT: Final = 100


class VerifyPort(Protocol):
    def probe(self, path: Path) -> FfprobeReport: ...

    def decode(self, path: Path) -> DecodeOutcome: ...

    def sha256(self, path: Path) -> str: ...


class Flags:
    def __init__(self) -> None:
        self.rows: list[VerifyMismatch] = []

    def add(self, code: str, detail: str) -> None:
        self.rows.append(VerifyMismatch(code=code, detail=detail))

    def outcome(self) -> VerifyOutcome:
        return VerifyOutcome(passed=not self.rows, mismatches=tuple(self.rows))
