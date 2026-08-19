"""Todo-58 acceptance: verified Fusion-title and external media-overlay paths.

Offline: the path chooser honors the probe-verified capability verdict with
EXPLICIT recorded fallbacks (never silent); missing font/template bindings,
wrong track/duration readback, API-only success without rendered pixels, and
arbitrary Fusion graph payloads are typed failures/blocks; the pinned-ffmpeg
external render is transparent, deterministic, and its rendered presence is
verified from PIXELS (region coverage at anchor frames). Live: the title
probe records the honest Fusion capability, and the end-to-end overlay build
verifies the final rendered placement from the actual render bytes.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RecordFrameSpan
from services.foundation_io import canonical_model_bytes
from services.presentation.overlay_models import (
    FusionSupport,
    OverlayControlRequest,
    OverlayControlValue,
    OverlayGeometry,
    OverlayItemRequest,
    OverlayPathDecision,
    OverlayPathError,
    OverlayPlacementReadback,
    OverlayRenderedMedia,
)
from services.presentation.overlay_paths import (
    build_overlay_section,
    choose_overlay_path,
    fusion_support_from_matrix,
    require_pixel_evidence,
    verify_placement_readback,
)
from services.presentation.overlay_render import (
    render_transparent_overlay,
    verify_rendered_presence,
)
from services.preview.tools import load_pinned_tools
from services.resolve_adapter.models import MediaBinding
from services.resolve_adapter.overlay_section import (
    overlay_media_bindings,
    overlay_placements,
)
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import PROJECT_PREFIX
from services.resolve_bridge.readiness import load_host_report
from services.resolve_bridge.title_probe import (
    append_matrix_findings,
    derive_matrix_findings,
    run_guarded_child,
)
from services.resolve_bridge.title_probe_models import (
    ControlProbe,
    PlacementReadback,
    TitleProbeReport,
)
from services.spike.gate_models import (
    ApiFindingEntry,
    CapabilityEntry,
    CapabilityMatrix,
    EvidenceRef,
)
from tests.resolve_adapter.support import (
    declared_media,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)

SPAN = RecordFrameSpan(start_frame=150, end_frame=240)
GEO = OverlayGeometry(
    timeline_width=1920,
    timeline_height=1080,
    safe_margin_px=96,
    region_width_px=480,
    region_height_px=270,
)
SHA = "a" * 64
PROBE_SHA = "b" * 64


def _support(*, supported: bool = True, template_sha: str | None = PROBE_SHA) -> FusionSupport:
    return FusionSupport(
        supported=supported,
        template_name="Text+",
        template_probe_sha256=template_sha,
        published_controls=("StyledText", "Size", "Center"),
    )


def _request(  # noqa: PLR0913 (test fixture builder)
    declared: str = "fusion_template",
    *,
    font: bool = True,
    role: str = "chapter_title",
    anchor: str = "top-right",
    span: RecordFrameSpan = SPAN,
    controls: tuple[tuple[str, str], ...] = (),
    graph: str | None = None,
) -> OverlayItemRequest:
    return OverlayItemRequest(
        item_id="title.chapter-1",
        role=role,  # type: ignore[arg-type]
        text="Chapter One",
        record_span=span,
        anchor=anchor,  # type: ignore[arg-type]
        asset_path="assets/p3-brand-a/logo.mov",
        asset_sha256=SHA,
        declared_path=declared,  # type: ignore[arg-type]
        font_family="Open Sans" if font else None,
        font_evidence_sha256=PROBE_SHA if font else None,
        fusion_controls=tuple(
            OverlayControlRequest(control_id=name, value=OverlayControlValue(text=value))
            for name, value in controls
        ),
        fusion_graph=graph,
    )


def _choose(request: OverlayItemRequest, support: FusionSupport) -> OverlayPathDecision:
    return choose_overlay_path(request, support, GEO)


def _rendered(item_id: str = "title.chapter-1") -> OverlayRenderedMedia:
    return OverlayRenderedMedia(
        item_id=item_id,
        path="evidence/overlay.mov",
        sha256=SHA,
        duration_frames=SPAN.length,
        expected_rgb=(253, 210, 0),
    )


# ---------------------------------------------------------------- chooser ----


def test_supported_fusion_or_external_fallback_passes() -> None:
    fusion = _choose(_request(controls=(("StyledText", "Chapter One"),)), _support())
    assert fusion.path == "fusion_template"
    assert fusion.reason == "profile_declared_fusion"
    assert fusion.template_name == "Text+"
    assert fusion.template_probe_sha256 == PROBE_SHA
    assert fusion.font_family == "Open Sans"
    assert [c.control_id for c in fusion.controls] == ["StyledText"]

    fallback = _choose(_request(), _support(supported=False))
    assert fallback.path == "external_media"
    assert fallback.reason == "fusion_unsupported_explicit_fallback"

    external = _choose(_request(declared="external_media"), _support())
    assert external.path == "external_media"
    assert external.reason == "profile_declared_external"


def test_complex_graph_request_is_blocked() -> None:
    request = _request(graph="TextPlus{ ... arbitrary lua graph ... }")
    with pytest.raises(OverlayPathError) as raised:
        _choose(request, _support())
    assert raised.value.code == "complex_graph_blocked"


def test_unsupported_control_falls_back_explicitly() -> None:
    request = _request(controls=(("StyledText", "Chapter One"), ("BlurAmount", "0.4")))
    decision = _choose(request, _support())
    assert decision.path == "external_media"
    assert decision.reason == "unsupported_control_explicit_fallback"
    assert decision.fallback_controls == ("BlurAmount",)


def test_missing_font_or_template_binding_is_typed() -> None:
    with pytest.raises(OverlayPathError) as raised:
        _choose(_request(font=False), _support())
    assert raised.value.code == "missing_font_binding"

    with pytest.raises(OverlayPathError) as raised:
        _choose(_request(), _support(template_sha=None))
    assert raised.value.code == "missing_template_binding"

    honest_fallback = _choose(_request(), _support(supported=False, template_sha=None))
    assert honest_fallback.path == "external_media"
    assert honest_fallback.reason == "fusion_unsupported_explicit_fallback"


# ------------------------------------------------------- readback + pixels ---


def test_wrong_track_or_duration_is_typed() -> None:
    decision = _choose(_request(declared="external_media"), _support(supported=False))
    readback = OverlayPlacementReadback(
        item_id=decision.item_id,
        track_type="video",
        track_index=1,
        record_start=108000 + SPAN.start_frame,
        record_end=108000 + SPAN.end_frame,
    )
    with pytest.raises(OverlayPathError) as raised:
        verify_placement_readback(decision, readback, track_index=2, frame_origin=108000)
    assert raised.value.code == "wrong_track"

    readback_ok_track = readback.model_copy(update={"track_index": 2, "record_end": 108301})
    with pytest.raises(OverlayPathError) as raised:
        verify_placement_readback(
            decision, readback_ok_track, track_index=2, frame_origin=108000
        )
    assert raised.value.code == "wrong_duration"


def test_api_only_success_without_render_fails() -> None:
    section = build_overlay_section(
        (_request(declared="external_media"),),
        _support(supported=False),
        GEO,
        rendered={"title.chapter-1": _rendered()},
        overlay_track_index=2,
    )
    decision = section.decisions[0]
    with pytest.raises(OverlayPathError) as raised:
        require_pixel_evidence(decision.item_id, pixels_verified=False, frames_extracted=0)
    assert raised.value.code == "api_only_success"

    require_pixel_evidence(
        decision.item_id, pixels_verified=True, frames_extracted=len(section.anchors)
    )


def test_section_binds_rendered_media_and_anchor_frames() -> None:
    section = build_overlay_section(
        (_request(declared="external_media"),),
        _support(supported=False),
        GEO,
        rendered={"title.chapter-1": _rendered()},
        overlay_track_index=2,
    )
    decision = section.decisions[0]
    assert decision.region.x == 1920 - 96 - 480
    assert decision.region.y == 96
    frames = {anchor.frame_index: anchor.expect_covered for anchor in section.anchors}
    assert frames == {149: False, 150: True, 239: True, 240: False}

    with pytest.raises(OverlayPathError) as raised:
        build_overlay_section(
            (_request(declared="external_media"),),
            _support(supported=False),
            GEO,
            rendered={},
            overlay_track_index=2,
        )
    assert raised.value.code == "overlay_binding_missing"


def test_region_out_of_frame_is_typed() -> None:
    with pytest.raises(OverlayPathError) as raised:
        OverlayGeometry(
            timeline_width=1920,
            timeline_height=1080,
            safe_margin_px=1600,
            region_width_px=480,
            region_height_px=270,
        ).region_for("top-right")
    assert raised.value.code == "rendered_region_out_of_frame"


# ------------------------------------------------------------ probe matrix ---


def _finding(name: str, *, verified: bool = True, limits: str = "probe limits") -> ApiFindingEntry:
    return ApiFindingEntry(
        finding=name,
        api_available=True,
        live_verified=verified,
        evidence_refs=(EvidenceRef(path="title-probe/title-probe-report.json", sha256=PROBE_SHA),),
        limitations=limits,
    )


def test_matrix_append_is_idempotent_and_conflict_typed(tmp_path: Path) -> None:
    matrix = CapabilityMatrix(
        schema_version="capability-matrix-v1",
        resolve_version="21.0.4",
        resolve_build="21.0.40005",
        capabilities=(
            CapabilityEntry(
                capability="base-cut-fixture",
                api_available=True,
                live_verified=True,
                evidence_refs=(
                    EvidenceRef(path="probes/capability-probes.json", sha256=PROBE_SHA),
                ),
                limitations="test fixture row",
            ),
        ),
        findings=(_finding("fusion-title-template-placement"),),
    )
    target = tmp_path / "title-probe-findings.json"
    target.write_bytes(json.dumps({}).encode() and _dump(matrix))
    append_matrix_findings(target, (_finding("fusion-title-template-placement"),))
    append_matrix_findings(target, (_finding("fusion-title-control-readback", limits="ctrl"),))
    appended = json.loads(target.read_bytes())
    assert [row["finding"] for row in appended["findings"]] == [
        "fusion-title-template-placement",
        "fusion-title-control-readback",
    ]
    with pytest.raises(ValueError, match="conflicts"):
        append_matrix_findings(
            target, (_finding("fusion-title-template-placement", verified=False),)
        )

    support = fusion_support_from_matrix(target)
    assert support.supported is True
    assert support.template_probe_sha256 == PROBE_SHA
    assert support.published_controls == ("StyledText", "Size", "Center")

    partial = tmp_path / "partial.json"
    append_matrix_findings(partial, (_finding("fusion-title-template-placement"),))
    assert fusion_support_from_matrix(partial).supported is False
    assert fusion_support_from_matrix(Path("/nonexistent/missing.json")).supported is False


def _dump(matrix: CapabilityMatrix) -> bytes:

    return canonical_model_bytes(matrix)


def test_probe_findings_derive_honestly_from_report() -> None:

    verified = TitleProbeReport(
        schema_version="title-probe-report-v1",
        binding="21.0.4.5",
        place_rung="insert_fusion_title",
        placed=True,
        structural=PlacementReadback(
            record_start=108000, record_end=108150, duration=150,
            track_type="video", track_index=1,
        ),
        comp_accessed=True,
        tool_found=True,
        controls=(
            ControlProbe(
                control="StyledText",
                write_value_text="FVP PROBE",
                read_before="Custom Title",
                write_returned="None",
                read_after="FVP PROBE",
                readback_matches=True,
            ),
        ),
        steps=(),
    )
    assert verified.fusion_supported is True
    findings = derive_matrix_findings(verified, report_sha=PROBE_SHA)
    assert [f.finding for f in findings] == [
        "fusion-title-template-placement",
        "fusion-title-control-readback",
    ]
    assert all(f.live_verified for f in findings)

    unverified = verified.model_copy(update={"tool_found": False})
    assert unverified.fusion_supported is False
    honest = derive_matrix_findings(unverified, report_sha=PROBE_SHA)
    assert [f.live_verified for f in honest] == [True, False]


def test_run_guarded_child_records_timeout(tmp_path: Path) -> None:
    result = tmp_path / "child-result.json"
    outcome = run_guarded_child(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        timeout_seconds=3,
        result_path=result,
    )
    assert outcome == "timeout"
    payload = json.loads(result.read_bytes())
    assert payload["watchdog"] == "timeout"


# ------------------------------------------------- pinned-ffmpeg rendering ---


@dataclass(frozen=True, slots=True)
class Pinned:
    ffmpeg: Path
    ffprobe: Path


@pytest.fixture(scope="module")
def tools() -> Pinned:
    try:
        pinned = load_pinned_tools(Path("config/toolchains/phase-0c-v1.json"))
    except Exception as error:  # noqa: BLE001 (skip on any toolchain failure)
        pytest.skip(f"pinned toolchain unavailable: {error}")
    return Pinned(ffmpeg=pinned.ffmpeg, ffprobe=pinned.ffprobe)


ASSET = Path("tests/fixtures/manifests/phase-3/assets/p3-brand-a/overlay.mov")
ASSET_SHA = "d680df082874622b9f7d4833b9e9b2ae43b8578e8a20d633d6333051163d64b2"


def test_external_overlay_render_is_transparent_and_deterministic(
    tools: Pinned, tmp_path: Path
) -> None:
    region = GEO.region_for("top-right")
    first = render_transparent_overlay(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        asset=ASSET,
        asset_sha256=ASSET_SHA,
        region=region,
        timeline_width=GEO.timeline_width,
        timeline_height=GEO.timeline_height,
        rate_num=30,
        duration_frames=SPAN.length,
        output=tmp_path / "overlay-a.mov",
    )
    assert first.nb_frames == SPAN.length
    assert first.pix_fmt.startswith("yuva")
    assert first.expected_rgb[2] == 0  # the brand-a overlay is yellow: blue ~ 0

    second = render_transparent_overlay(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        asset=ASSET,
        asset_sha256=ASSET_SHA,
        region=region,
        timeline_width=GEO.timeline_width,
        timeline_height=GEO.timeline_height,
        rate_num=30,
        duration_frames=SPAN.length,
        output=tmp_path / "overlay-b.mov",
    )
    assert first.sha256 == second.sha256

    with pytest.raises(OverlayPathError) as raised:
        render_transparent_overlay(
            ffmpeg_bin=tools.ffmpeg,
            ffprobe_bin=tools.ffprobe,
            asset=ASSET,
            asset_sha256="0" * 64,
            region=region,
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=30,
            duration_frames=SPAN.length,
            output=tmp_path / "overlay-c.mov",
        )
    assert raised.value.code == "overlay_media_hash_drift"


def test_rendered_presence_is_verified_from_pixels_and_typed_on_drift(
    tools: Pinned, tmp_path: Path
) -> None:
    region = GEO.region_for("top-right")
    overlay = render_transparent_overlay(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        asset=ASSET,
        asset_sha256=ASSET_SHA,
        region=region,
        timeline_width=GEO.timeline_width,
        timeline_height=GEO.timeline_height,
        rate_num=30,
        duration_frames=SPAN.length,
        output=tmp_path / "overlay.mov",
    )
    base = _composite_fixture(tools, tmp_path / "final.mp4", overlay.path)
    section = build_overlay_section(
        (_request(declared="external_media"),),
        _support(supported=False),
        GEO,
        rendered={
            "title.chapter-1": OverlayRenderedMedia(
                item_id="title.chapter-1",
                path=str(overlay.path),
                sha256=overlay.sha256,
                duration_frames=SPAN.length,
                expected_rgb=overlay.expected_rgb,
            )
        },
        overlay_track_index=2,
    )
    decision = section.decisions[0]
    checks = verify_rendered_presence(
        ffmpeg_bin=tools.ffmpeg,
        render_path=base,
        width=GEO.timeline_width,
        height=GEO.timeline_height,
        decision=decision,
        anchors=section.anchors,
        expected_rgb=overlay.expected_rgb,
    )
    assert all(check.passed for check in checks), [c.detail for c in checks]

    wrong_region_decision = decision.model_copy(
        update={"region": GEO.region_for("bottom-left")}
    )
    with pytest.raises(OverlayPathError) as raised:
        verify_rendered_presence(
            ffmpeg_bin=tools.ffmpeg,
            render_path=base,
            width=GEO.timeline_width,
            height=GEO.timeline_height,
            decision=wrong_region_decision,
            anchors=section.anchors,
            expected_rgb=overlay.expected_rgb,
        )
    assert raised.value.code == "rendered_presence_missing"

    late_span = RecordFrameSpan(start_frame=400, end_frame=490)
    late_anchors = build_overlay_section(
        (_request(declared="external_media", span=late_span),),
        _support(supported=False),
        GEO,
        rendered={
            "title.chapter-1": OverlayRenderedMedia(
                item_id="title.chapter-1",
                path=str(overlay.path),
                sha256=overlay.sha256,
                duration_frames=late_span.length,
                expected_rgb=overlay.expected_rgb,
            )
        },
        overlay_track_index=2,
    ).anchors
    with pytest.raises(OverlayPathError) as raised:
        verify_rendered_presence(
            ffmpeg_bin=tools.ffmpeg,
            render_path=base,
            width=GEO.timeline_width,
            height=GEO.timeline_height,
            decision=decision.model_copy(update={"record_span": late_span}),
            anchors=late_anchors,
            expected_rgb=overlay.expected_rgb,
        )
    assert raised.value.code == "rendered_presence_missing"


def _composite_fixture(tools: Pinned, output: Path, overlay: Path) -> Path:
    """Base bars + overlay limited to the span, like the real render would show."""

    argv = (
        str(tools.ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:r=30:duration=20",
        "-stream_loop",
        "-1",
        "-i",
        str(overlay),
        "-filter_complex",
        "[0:v][1:v]overlay=0:0:enable='between(t,5,7.999)',format=yuv420p[v]",
        "-map",
        "[v]",
        "-frames:v",
        "600",
        "-c:v",
        "h264_videotoolbox",
        "-map_metadata",
        "-1",
        str(output),
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return output


# ------------------------------------------------------------ package wire ---


def test_package_carries_overlay_section_and_external_placements() -> None:

    manifest = load_p2_manifest("p2-partial-build-restart")
    ir = ir_for(manifest)
    section = build_overlay_section(
        (_request(declared="external_media"),),
        _support(supported=False),
        GEO,
        rendered={"title.chapter-1": _rendered()},
        overlay_track_index=2,
    )
    bindings = overlay_media_bindings(section)
    assert len(bindings) == 1
    assert bindings[0].source_id == "overlay.title.chapter-1"
    placements = overlay_placements(section, frame_origin=108000)
    assert placements[0].clip_info.track_type == "video"
    assert placements[0].clip_info.track_index == 2
    assert placements[0].clip_info.record_frame == 108000 + SPAN.start_frame
    assert placements[0].clip_info.end_frame - placements[0].clip_info.start_frame == (
        SPAN.length
    )

    assert MediaBinding.model_validate(bindings[0].model_dump(mode="json"))

    declared = declared_media(manifest)
    presented = (*declared, *bindings)
    request = PackageCompileRequest(
        ir=ir,
        lock=phase2_lock(),
        lock_sha256=lock_sha256(),
        declared_media=declared,
        presented_media=presented,
        artifact_id="resolve-package-overlay-test",
    )
    package = compile_resolve_package(request)
    assert package.overlay_paths is None
    with_overlay = compile_resolve_package(
        dataclasses.replace(request, overlay_paths=section, presented_media=presented)
    )
    assert with_overlay.overlay_paths is not None
    overlay_ids = {p.item_id for p in with_overlay.placements} - {
        p.item_id for p in package.placements
    }
    assert overlay_ids == {"title.chapter-1"}


def test_overlay_request_rejects_malformed_payloads(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        OverlayItemRequest(
            item_id="bad item!",
            role="chapter_title",
            text="x",
            record_span=SPAN,
            anchor="top-right",
            asset_path="a.mov",
            asset_sha256=SHA,
            declared_path="external_media",
        )
    with pytest.raises(OverlayPathError) as raised:
        render_transparent_overlay(
            ffmpeg_bin=Path("/nonexistent-ffmpeg"),
            ffprobe_bin=Path("/nonexistent-ffprobe"),
            asset=ASSET,
            asset_sha256="0" * 64,
            region=GEO.region_for("top-right"),
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=30,
            duration_frames=SPAN.length,
            output=tmp_path / "never.mov",
        )
    assert raised.value.code == "overlay_media_hash_drift"


# ------------------------------------------------------------------- live ----


ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
EVIDENCE_NAME = "resolve"


def _report_path(config: pytest.Config) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    evidence = config.getoption("--resolve-evidence")
    if evidence:
        candidate = Path(str(evidence)).resolve().parent / "resolve-host.json"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class LiveEnv:
    report: Path
    evidence: Path
    ffmpeg: Path
    ffprobe: Path
    fixture_dir: Path


@pytest.fixture(scope="session")
def live_env(request: pytest.FixtureRequest) -> Iterator[LiveEnv]:
    report_path = _report_path(request.config)
    if report_path is None:
        pytest.skip("resolve host report not found: pass --resolve-evidence")
    evidence = Path(str(request.config.getoption("--resolve-evidence"))) if (
        request.config.getoption("--resolve-evidence")
    ) else None
    if evidence is None:
        pytest.skip("live requires --resolve-evidence")
    if request.config.getoption("--exclusive-resolve-lease"):
        lock_path = evidence / "resolve-lease.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pytest.skip("exclusive resolve lease held by another session")
        print(f"exclusive resolve lease acquired: {lock_path}")
        try:
            yield _live_env(report_path, evidence)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        return
    yield _live_env(report_path, evidence)


def _live_env(report_path: Path, evidence: Path) -> LiveEnv:
    ffmpeg = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffmpeg"
    ffprobe = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffprobe"
    fixture_dir = evidence.parent / "phase-0a/fixture"
    missing = [
        str(path)
        for path in (ffmpeg, ffprobe, fixture_dir / "source.mov")
        if not Path(path).is_file()
    ]
    if missing:
        pytest.skip(f"live pinned tools or fixture media missing: {missing}")
    return LiveEnv(
        report=report_path,
        evidence=evidence,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        fixture_dir=fixture_dir,
    )


@dataclass(frozen=True, slots=True)
class ProbeRun:
    stdout: str
    report: dict[str, object]


@pytest.fixture(scope="session")
def probe_run(live_env: LiveEnv) -> ProbeRun:
    bundle = live_env.evidence / "title-probe"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.resolve_bridge.title_probe",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--timeout",
        "240",
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((bundle / "title-probe-report.json").read_bytes())
    return ProbeRun(stdout=result.stdout, report=report)


@pytest.fixture(scope="session")
def overlay_live_run(
    live_env: LiveEnv, probe_run: ProbeRun
) -> subprocess.CompletedProcess[str]:
    bundle = live_env.evidence / "overlay-live"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.presentation.overlay_live",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--ffmpeg",
        str(live_env.ffmpeg),
        "--ffprobe",
        str(live_env.ffprobe),
        "--base-source",
        str(live_env.fixture_dir / "source.mov"),
        "--overlay-asset",
        str(ASSET),
        "--timeout",
        "900",
    )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1200)


@pytest.mark.resolve_live
def test_live_title_probe_records_honest_capability(probe_run: ProbeRun) -> None:
    assert "title-probe: fusion_supported=" in probe_run.stdout
    report = probe_run.report
    controls = report["controls"]
    assert isinstance(controls, list)
    supported = report["fusion_supported"] is True
    if supported:
        assert report["placed"] is True
        assert report["tool_found"] is True
        assert all(row["readback_matches"] for row in controls)
        assert {row["control"] for row in controls} == {"StyledText", "Size", "Center"}
    else:
        assert not all(
            isinstance(row.get("readback_matches"), bool) and row["readback_matches"]
            for row in controls
        )
    findings = json.loads(
        (live_evidence_root() / "title-probe-findings.json").read_bytes()
    ) if (live_evidence_root() / "title-probe-findings.json").is_file() else None
    del findings


def live_evidence_root() -> Path:
    return ATTEMPT / EVIDENCE_NAME


@pytest.mark.resolve_live
def test_live_overlay_paths_end_to_end(overlay_live_run: subprocess.CompletedProcess[str],
                                       probe_run: ProbeRun) -> None:
    result = overlay_live_run
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "overlay-live: PASS" in result.stdout
    assert "path=external_media" in result.stdout
    assert "rendered-presence=verified" in result.stdout
    supported = probe_run.report["fusion_supported"] is True
    if supported:
        assert "fusion label=fusion_applied" in result.stdout
        assert "fusion-controls-verified=3/3" in result.stdout
    else:
        assert "fusion label=explicit_fallback" in result.stdout
    report = json.loads(
        (live_evidence_root() / "overlay-live" / "overlay-live-report.json").read_bytes()
    )
    assert report["passed"] is True
    assert report["rendered_presence"] == "verified"
    assert Path(str(report["render"]["output_path"])).is_file()


@pytest.mark.resolve_live
def test_live_cleanup_leaves_no_owned_projects(live_env: LiveEnv) -> None:

    connection = connect(load_host_report(live_env.report))
    manager = connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"overlay-live leaked owned projects: {owned}"
