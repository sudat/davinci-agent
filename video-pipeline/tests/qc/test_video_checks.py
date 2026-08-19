"""Video checks over real pinned-ffmpeg synthesized renders.

Fault fixtures are REAL media: a mid-render black segment, a frozen (static
gray) segment, a truncated/undecodable file, and metadata mismatches from a
declared preset that disagrees with the actual file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.qc.checks.video_checks import VideoCheckRequest, check_video, metadata_issues
from services.qc.issue_factory import IssueFactory
from services.qc.tools import load_qc_tools, parse_probe_report
from services.qc.video_probe import PinnedBlackFreezeProbe
from tests.qc.support import (
    BLACK_SOURCES,
    FREEZE_SOURCES,
    RenderSpec,
    base_render,
    clean_policy,
    preset,
)

INPUTS = ("f" * 64,)


def rules(issues) -> set[str]:
    return {issue.rule_id for issue in issues}


def _no_spans(
    render: Path, min_duration_ms: int, video_duration_ms: int
) -> tuple[()]:
    del render, min_duration_ms, video_duration_ms
    return ()


def _run_video_checks(tmp_path: Path, spec: RenderSpec, policy_of, name: str) -> tuple:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, spec, name=name)
    report, _raw = parse_probe_report(tools.probe_raw(render))
    probe = PinnedBlackFreezeProbe(tools)
    return check_video(
        VideoCheckRequest(
            render=render,
            decode_outcome=tools.decode(render),
            report=report,
            policy=policy_of(spec),
            inputs=INPUTS,
            ffmpeg_sha256=tools.ffmpeg_sha256,
            black_source=probe.black,
            freeze_source=probe.freeze,
        )
    )


def test_clean_render_passes_video_checks(tmp_path: Path) -> None:
    issues = _run_video_checks(
        tmp_path,
        RenderSpec(),
        lambda spec: clean_policy(preset(spec, nb_frames="120")),
        "clean.mp4",
    )
    assert issues == ()


def test_black_segment_blocks(tmp_path: Path) -> None:
    spec = RenderSpec(video_sources=BLACK_SOURCES)
    issues = _run_video_checks(
        tmp_path, spec, lambda spec: clean_policy(preset(spec)), "black.mp4"
    )
    assert "video_black_span" in rules(issues)
    issue = next(i for i in issues if i.rule_id == "video_black_span")
    assert any(m.name == "black_start_ms" for m in issue.evidence.measured)


def test_frozen_segment_blocks(tmp_path: Path) -> None:
    spec = RenderSpec(video_sources=FREEZE_SOURCES)
    issues = _run_video_checks(
        tmp_path, spec, lambda spec: clean_policy(preset(spec)), "freeze.mp4"
    )
    assert "video_freeze_span" in rules(issues)


def test_corrupt_render_fails_decode(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="good.mp4")
    corrupt = tmp_path / "corrupt.mp4"
    payload = render.read_bytes()
    corrupt.write_bytes(payload[: len(payload) // 3])
    report, _raw = parse_probe_report(tools.probe_raw(render))
    issues = check_video(
        VideoCheckRequest(
            render=corrupt,
            decode_outcome=tools.decode(corrupt),
            report=report,
            policy=clean_policy(preset(RenderSpec())),
            inputs=INPUTS,
            ffmpeg_sha256=tools.ffmpeg_sha256,
            black_source=_no_spans,
            freeze_source=_no_spans,
        )
    )
    assert "video_decode_failed" in rules(issues)


def test_metadata_mismatch_blocks(tmp_path: Path) -> None:
    issues = _run_video_checks(
        tmp_path,
        RenderSpec(),
        lambda spec: clean_policy(preset(RenderSpec(width=999, height=111))),
        "meta.mp4",
    )
    assert "video_metadata_mismatch" in rules(issues)
    assert any("width" in issue.detail for issue in issues)


def test_raw_luma_fallback_matches_filter_path(tmp_path: Path) -> None:
    """Forcing the Todo-35 fallback (no filters) still finds the black span."""

    tools = load_qc_tools()
    spec = RenderSpec(video_sources=BLACK_SOURCES)
    render = base_render(tools.ffmpeg, tmp_path, spec, name="black-fallback.mp4")
    probe = PinnedBlackFreezeProbe(tools)
    probe._filters = frozenset()
    spans = probe.black(render, 1000, 5000)
    assert any(span.duration_ms >= 2000 for span in spans)


@pytest.mark.parametrize(
    ("field", "value"),
    [("width", 1280), ("r_frame_rate", "25/1"), ("pix_fmt", "yuv422p")],
)
def test_each_metadata_field_drift_is_reported(
    tmp_path: Path, field: str, value: object
) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="fields.mp4")
    report, _raw = parse_probe_report(tools.probe_raw(render))
    policy = clean_policy(preset(RenderSpec()).model_copy(update={field: value}))
    issues, _duration = metadata_issues(
        report, policy, IssueFactory.for_policy(policy, INPUTS)
    )
    assert issues
    assert all(issue.rule_id == "video_metadata_mismatch" for issue in issues)
