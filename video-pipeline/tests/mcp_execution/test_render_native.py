"""Task 7 — native render lifecycle via plan step (typed)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import services.mcp_execution.native_render_probe as nrp
from services.foundation_io import sha256_file
from services.mcp_execution import native_render_media as nrm
from services.mcp_execution.live_handlers.common import LiveAdapterError, LiveSessionContext
from services.mcp_execution.live_handlers.render import render_native
from services.mcp_execution.native_render_media import (
    probe_video_frame_count,
    validate_native_media,
)
from services.mcp_execution.native_render_meta import NativeRenderMeta

_PARENTS2 = Path(__file__).resolve().parents[2]
REPO = _PARENTS2.parent if (_PARENTS2 / "services").is_dir() else _PARENTS2
_VENV = REPO / "video-pipeline" / ".venv" if (REPO / "video-pipeline").is_dir() else REPO / ".venv"
FFMPEG = _VENV / "bin" / "ffmpeg"
FFPROBE = _VENV / "bin" / "ffprobe"


def _encode(path: Path, size: str, rate: int, *, no_audio: bool = False) -> None:
    ffmpeg = str(FFMPEG) if FFMPEG.is_file() else "ffmpeg"
    argv = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate={rate}:duration=1",
    ]
    if not no_audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
    argv += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
    ]
    if not no_audio:
        argv += ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
    argv += ["-shortest", str(path)]
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.skip(f"ffmpeg fixture unavailable: {result.stderr[-200:]}")


def _real_mp4(path: Path) -> Path:
    _encode(path, "1920x1080", 30)
    return path



def _ffprobe_bin() -> Path:
    if FFPROBE.is_file():
        return FFPROBE
    found = shutil.which("ffprobe")
    assert found is not None
    return Path(found)


def _trusted_meta(path: Path, name: str, job_id: str) -> NativeRenderMeta:
    """Measured-facts meta built from the real fixture via strict ffprobe."""

    probe = validate_native_media(
        path, _ffprobe_bin(), 1920, 1080, 30.0, "H264", "aac", 2, 48000
    )
    return NativeRenderMeta(
        schema_version="native-render-meta-v1",
        custom_name=name,
        job_id=job_id,
        output_path=str(path),
        output_sha256=sha256_file(path),
        duration_seconds=probe.duration_seconds,
        video_codec=probe.video_codec,
        width=probe.width,
        height=probe.height,
        avg_frame_rate=probe.avg_frame_rate,
        audio_codec=probe.audio_codec,
        audio_channels=probe.audio_channels,
        audio_sample_rate=probe.audio_sample_rate,
        has_subtitle_stream=probe.has_subtitle_stream,
    )


class ScriptedTransport:
    REAL_TOOLS = frozenset({"render", "project_manager", "timeline"})

    def __init__(
        self,
        script: dict[tuple[str, str], list[dict[str, object]]],
        on_start: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._script: dict[tuple[str, str], list[dict[str, object]]] = {
            k: list(v) for k, v in script.items()
        }
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.timeouts: list[float | None] = []
        self._on_start = on_start  # type: ignore[assignment]
        self._capture: dict[str, str] = {}

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append((tool_name, action, dict(normalized_params)))
        self.timeouts.append(timeout_seconds)
        if tool_name == "render" and action == "prepare_render_job":
            self._capture["target_dir"] = str(normalized_params.get("target_dir", ""))
            self._capture["custom_name"] = str(normalized_params.get("custom_name", ""))
        if tool_name == "render" and action == "start" and self._on_start is not None:
            cb = self._on_start
            cb(self._capture)
        key = (tool_name, action)
        if key not in self._script or not self._script[key]:
            raise AssertionError(f"unexpected {key} {normalized_params}")
        queue = self._script[key]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _ok(extra: dict[str, object] | None = None) -> dict[str, object]:
    base: dict[str, object] = {"success": True}
    if extra:
        base.update(extra)
    return base


def _valid_single(key: str, value: object) -> dict[str, object]:
    return {"success": True, "valid": True, "settings": {key: value}, "errors": [], "warnings": []}


def _safe_ok(key: str, value: object) -> dict[str, object]:
    return {
        "success": True,
        "validation": {"valid": True},
        "before": {},
        "after": {key: value},
        "diff": {"matched": [key]},
    }


def _prepare_echo(target: Path) -> dict[str, object]:
    return {
        "success": True,
        "job_id": "job-native-123",
        "settings": {
            "TargetDir": str(target),
            "CustomName": "finishing-native-ep-test",
            "SelectAllFrames": True,
            "ExportVideo": True,
            "ExportAudio": True,
            "DataBurnIn": "None",
            "FormatWidth": 1920,
            "FormatHeight": 1080,
            "FrameRate": 30,
        },
    }


def _script_success(tmp_path: Path) -> dict[tuple[str, str], list[dict[str, object]]]:
    """Scripted happy path; prepare echoes the full requested settings."""

    ordered_keys = [
        "TargetDir",
        "CustomName",
        "SelectAllFrames",
        "ExportVideo",
        "ExportAudio",
        "DataBurnIn",
        "FormatWidth",
        "FormatHeight",
        "FrameRate",
    ]
    vals: list[object] = [
        str(tmp_path),
        "finishing-native-ep-test",
        True,
        True,
        True,
        "None",
        1920,
        1080,
        30,
    ]
    script: dict[tuple[str, str], list[dict[str, object]]] = {
        ("render", "list_jobs"): [{"success": True, "jobs": []}],
        ("render", "is_rendering"): [{"success": True, "rendering": False}],
        ("render", "get_formats"): [
            {"success": True, "formats": {"mp4": "mp4", "mov": "mov"}}
        ],
        ("render", "get_codecs"): [
            {"success": True, "codecs": {"H.264": "H264", "H.265": "H265"}}
        ],
        ("render", "get_format_and_codec"): [
            {"success": True, "format": "mp4", "codec": "H264"}
        ],
        ("render", "set_format_and_codec"): [_ok()],
        ("render", "prepare_render_job"): [_prepare_echo(tmp_path)],
        ("render", "start"): [_ok()],
        ("render", "get_job_status"): [
            {
                "success": True,
                "JobStatus": "Complete",
                "CompletionPercentage": 100,
                "IsRenderingInProgress": False,
            }
        ],
        ("render", "delete_job"): [_ok()],
    }
    for k, v in zip(ordered_keys, vals, strict=True):
        script.setdefault(("render", "validate_render_settings"), []).append(_valid_single(k, v))
        script.setdefault(("render", "safe_set_render_settings"), []).append(_safe_ok(k, v))
    return script


def _render_params(custom_name: str = "finishing-native-ep-test") -> dict[str, object]:
    return {
        "action": "render_native",
        "custom_name": custom_name,
        "format_id": "mp4",
        "codec_id": "H264",
        "width": 1920,
        "height": 1080,
        "frame_rate": 30.0,
        "select_all_frames": True,
        "export_video": True,
        "export_audio": True,
        "data_burn_in": "None",
    }


def test_render_native_success(tmp_path: Path) -> None:
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        out = td / f"{name}.mp4"
        _real_mp4(out)

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["job_id"] == "job-native-123"
    out = Path(str(result["output_path"]))
    assert out.is_file()
    assert out.stat().st_size > 0
    assert result["output_sha256"] == sha256_file(out)
    assert any(c[0] == "render" and c[1] == "delete_job" for c in transport.calls)
    # Poll/witness/listing calls go through render_poll and must carry the
    # named measured operation deadline, not the 10s transport default.
    from services.mcp_execution.live_handlers.render_poll import (  # noqa: PLC0415
        RENDER_OPERATION_TIMEOUT_SECONDS,
    )

    poll_rows = [
        timeout
        for call, timeout in zip(transport.calls, transport.timeouts, strict=True)
        if call[0] == "render" and call[1] in {"get_job_status", "is_rendering", "list_jobs"}
    ]
    assert poll_rows, "the native lifecycle must exercise poll/witness calls"
    assert set(poll_rows) == {RENDER_OPERATION_TIMEOUT_SECONDS}


def test_rerun_reuses_same_output(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    # First run creates file.
    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    first = render_native(ctx, "render_native", _render_params())
    assert first["reused"] is False
    # Second run with the SAME custom_name must reuse the existing output
    # and queue zero new jobs (rerun duplicate/alternate-output guard).
    script2: dict[tuple[str, str], list[dict[str, object]]] = {
        ("render", "list_jobs"): [{"success": True, "jobs": []}],
        ("render", "is_rendering"): [{"success": True, "rendering": False}],
    }
    transport2 = ScriptedTransport(script2)
    ctx2 = LiveSessionContext.build(transport2, {}, render_dir=str(tmp_path))
    second = render_native(ctx2, "render_native", _render_params())
    assert second["reused"] is True
    assert second["output_path"] == first["output_path"]
    assert not any(c[1] == "prepare_render_job" for c in transport2.calls)
    assert not any(c[1] == "start" for c in transport2.calls)


def test_reuse_refused_after_session_timeline_mutation(tmp_path: Path) -> None:
    """Task 8 render currency: when this adapter session already mutated
    the timeline (placement/subtitle/grade/audio), a trusted prior render
    predates the current timeline state — reusing it would report a stale
    output as the episode's native render. The handler must re-render."""

    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    first = render_native(ctx, "render_native", _render_params())
    assert first["reused"] is False

    ctx.mark_timeline_mutated()
    mutated_script = _script_success(tmp_path)

    def on_start2(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        target = td / f"{name}.mp4"
        target.unlink(missing_ok=True)
        _real_mp4(target)

    transport2 = ScriptedTransport(mutated_script, on_start=on_start2)
    ctx.transport = transport2
    second = render_native(ctx, "render_native", _render_params())
    assert second["reused"] is False, "a render that predates a session mutation is stale"
    assert any(c[1] == "prepare_render_job" for c in transport2.calls)
    assert any(c[1] == "start" for c in transport2.calls)
    assert second["job_id"] == "job-native-123"
    assert Path(str(second["output_path"])).is_file()
    meta = json.loads(_meta_path(tmp_path).read_bytes())
    assert meta["job_id"] == "job-native-123"
    assert meta["output_sha256"] == sha256_file(
        tmp_path / "finishing-native-ep-test.mp4"
    )


def _meta_path(tmp_path: Path, name: str = "finishing-native-ep-test") -> Path:
    return tmp_path / f"{name}.meta.json"


def test_reuse_requires_trusted_metadata(tmp_path: Path) -> None:
    """An output without a trusted prior metadata record is untrusted:
    the handler must take the fresh-render path, never reuse it."""

    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    _real_mp4(out)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    assert any(c[1] == "prepare_render_job" for c in transport.calls)


def test_reuse_rejects_tampered_hash(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    _real_mp4(out)
    meta = _trusted_meta(out, "finishing-native-ep-test", "job-native-prior")
    tampered = meta.model_copy(update={"output_sha256": "0" * 64})
    out.parent.joinpath(_meta_path(tmp_path).name).write_text(tampered.model_dump_json())

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    assert any(c[1] == "prepare_render_job" for c in transport.calls)


def test_reuse_rejects_sentinel_job_id(tmp_path: Path) -> None:
    """The 'reused-existing' sentinel is structurally rejected by the meta
    model; a file carrying it is untrusted and takes the fresh path."""

    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    _real_mp4(out)
    raw = json.loads(_trusted_meta(out, "finishing-native-ep-test", "x").model_dump_json())
    raw["job_id"] = "reused-existing"
    _meta_path(tmp_path).write_text(json.dumps(raw))

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    assert any(c[1] == "prepare_render_job" for c in transport.calls)


def test_rerun_returns_real_prior_job_id_and_measured_facts(
    tmp_path: Path,
) -> None:
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    first = render_native(ctx, "render_native", _render_params())

    script2: dict[tuple[str, str], list[dict[str, object]]] = {
        ("render", "list_jobs"): [{"success": True, "jobs": []}],
        ("render", "is_rendering"): [{"success": True, "rendering": False}],
    }
    transport2 = ScriptedTransport(script2)
    ctx2 = LiveSessionContext.build(transport2, {}, render_dir=str(tmp_path))
    second = render_native(ctx2, "render_native", _render_params())
    assert second["reused"] is True
    assert second["job_id"] == first["job_id"] == "job-native-123"
    assert second["job_id"] != "reused-existing"
    assert second["output_path"] == first["output_path"]
    assert second["output_sha256"] == first["output_sha256"]
    media = second["media"]
    assert isinstance(media, dict)
    assert media["duration_seconds"] > 0
    assert media["video_codec"]
    assert media["width"] == 1920
    assert media["height"] == 1080
    assert not any(c[1] == "prepare_render_job" for c in transport2.calls)
    assert not any(c[1] == "start" for c in transport2.calls)


def test_format_readback_missing_fields_fail(tmp_path: Path) -> None:
    """A get_format_and_codec readback without both identifiers fails closed."""

    script = _script_success(tmp_path)
    script[("render", "get_format_and_codec")] = [{"success": True}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-format-codec-readback-missing"):
        render_native(ctx, "render_native", _render_params())


def test_stale_exact_file_is_never_accepted_as_fresh(tmp_path: Path) -> None:
    """A pre-existing exact-path file whose mtime predates the job start is
    never accepted as this job's output (no permissive fallback)."""

    import time as _time  # noqa: PLC0415

    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    _real_mp4(out)
    old = _time.time() - 10_000
    os.utime(out, (old, old))

    transport = ScriptedTransport(script, on_start=lambda cap: None)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-output-missing"):
        render_native(ctx, "render_native", _render_params())


def test_global_preflight_called_even_with_empty_job_list(
    tmp_path: Path,
) -> None:
    """An empty list_jobs is not proof rendering stopped: the pinned MCP
    global render-state action must be called unconditionally."""

    script = _script_success(tmp_path)
    script[("render", "is_rendering")] = [{"success": True, "rendering": False}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    preflight = [c for c in transport.calls if c[1] == "is_rendering"]
    assert len(preflight) >= 1


def test_global_preflight_true_refuses_even_with_empty_jobs(
    tmp_path: Path,
) -> None:
    script = _script_success(tmp_path)
    script[("render", "is_rendering")] = [{"success": True, "rendering": True}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-already-in-progress"):
        render_native(ctx, "render_native", _render_params())
    assert not any(c[1] == "prepare_render_job" for c in transport.calls)


def test_global_preflight_malformed_fails_typed(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    script[("render", "is_rendering")] = [{"success": True}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-global-state-missing"):
        render_native(ctx, "render_native", _render_params())


def test_global_state_non_boolean_fails_typed(tmp_path: Path) -> None:
    """A non-boolean rendering value must become a typed adapter error with
    the raw payload in the diagnostic — never a raw ValidationError — and
    nothing may be queued."""

    script = _script_success(tmp_path)
    script[("render", "is_rendering")] = [{"success": True, "rendering": "false"}]
    transport = ScriptedTransport(script)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-global-state-malformed"):
        render_native(ctx, "render_native", _render_params())
    assert not any(c[1] == "prepare_render_job" for c in transport.calls)
    assert not any(c[1] == "start" for c in transport.calls)


def test_exact_progress_100_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CompletionPercentage == 100 exactly; 99 with a false flag never does."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [
        {
            "success": True,
            "JobStatus": "Almost",
            "CompletionPercentage": 99,
            "IsRenderingInProgress": False,
        }
    ]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-timeout"):
        render_native(ctx, "render_native", _render_params())


def test_failed_job_payload_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vendor's PascalCase Error payload fails the step at once."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [
        {
            "success": True,
            "JobStatus": "失敗しました",
            "CompletionPercentage": 70,
            "Error": "デコードエラー",
        }
    ]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match=r"render-job-failed.*デコードエラー"):
        render_native(ctx, "render_native", _render_params())


def test_reuse_invalid_media_falls_through_to_fresh(tmp_path: Path) -> None:
    """A trusted-looking record over corrupt bytes must take the fresh path,
    never escape as a terminal media-validation exception."""

    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00" * 512)
    raw_meta = {
        "schema_version": "native-render-meta-v1",
        "custom_name": "finishing-native-ep-test",
        "job_id": "job-native-prior",
        "output_path": str(out),
        "output_sha256": sha256_file(out),
        "duration_seconds": 1.0,
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "30/1",
        "audio_codec": "aac",
        "audio_channels": 2,
        "audio_sample_rate": 48000,
        "has_subtitle_stream": False,
    }
    _meta_path(tmp_path).write_text(json.dumps(raw_meta))

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    assert any(c[1] == "prepare_render_job" for c in transport.calls)


def test_subtitle_stream_fact_tamper_falls_through_to_fresh(
    tmp_path: Path,
) -> None:
    script = _script_success(tmp_path)
    out = tmp_path / "finishing-native-ep-test.mp4"
    _real_mp4(out)
    meta = _trusted_meta(out, "finishing-native-ep-test", "job-native-prior")
    tampered = meta.model_copy(update={"has_subtitle_stream": True})
    _meta_path(tmp_path).write_text(tampered.model_dump_json())

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["reused"] is False
    assert any(c[1] == "prepare_render_job" for c in transport.calls)


def test_cli_direct_mutation_is_blocked() -> None:
    base = REPO / "video-pipeline" if (REPO / "video-pipeline").is_dir() else REPO
    source = (base / "services" / "cli" / "_v44_finishing_run.py").read_text()
    assert "live_handlers" not in source
    assert "render_native_finishing" not in source
    assert "LiveMcpAdapter(" not in source


def test_malformed_list_jobs(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    script[("render", "list_jobs")] = [{"success": True, "jobs": "not-a-tuple"}]  # type: ignore[list-item]
    transport = ScriptedTransport(script)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-list-malformed"):
        render_native(ctx, "render_native", _render_params())


def test_job_settings_readback_mismatch(tmp_path: Path) -> None:
    """A queued job whose settings readback drops a key fails typed."""
    script = _script_success(tmp_path)
    echo = _prepare_echo(tmp_path)
    settings_map: dict[str, object] = dict(echo["settings"])  # type: ignore[arg-type]
    settings_map.pop("ExportAudio", None)
    echo["settings"] = settings_map
    script[("render", "prepare_render_job")] = [echo]
    transport = ScriptedTransport(script)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-job-settings-mismatch"):
        render_native(ctx, "render_native", _render_params())
    assert not any(c[1] == "start" for c in transport.calls)


def test_wrong_codec(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    script[("render", "get_codecs")] = [{"success": True, "codecs": {"H.265": "H265"}}]
    transport = ScriptedTransport(script)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-codec-not-found"):
        render_native(ctx, "render_native", _render_params())


def test_invalid_duration(tmp_path: Path) -> None:
    """A zero-byte/invalid output is refused before delete is ever called."""
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        (td / f"{name}.mp4").write_bytes(b"\x00" * 64)

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-"):
        render_native(ctx, "render_native", _render_params())
    assert not any(c[1] == "delete_job" for c in transport.calls)


def test_delete_failure_loud(tmp_path: Path) -> None:
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    script[("render", "delete_job")] = [
        {"success": False, "error": {"message": "cannot delete"}}
    ]
    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-delete-failed"):
        render_native(ctx, "render_native", _render_params())


def test_zero_bytes_refused(tmp_path: Path) -> None:
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        (td / f"{name}.mp4").write_bytes(b"")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(
        LiveAdapterError, match=r"render-output-zero-bytes|render-output-missing"
    ):
        render_native(ctx, "render_native", _render_params())


def _status_without_flag() -> dict[str, object]:
    """The measured Resolve 21.0.4.5 completion payload: no per-job flag."""

    return {"success": True, "JobStatus": "Complete", "CompletionPercentage": 100}


def test_poll_completes_via_global_state_when_per_job_flag_absent(
    tmp_path: Path,
) -> None:
    """Per-job flag absent + pinned MCP is_rendering false in the SAME poll
    cycle completes the exact job (single-writer one-job precondition)."""

    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [_status_without_flag()]
    script[("render", "is_rendering")] = [{"success": True, "rendering": False}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["job_id"] == "job-native-123"
    polls = [c for c in transport.calls if c[1] == "get_job_status"]
    globals_ = [c for c in transport.calls if c[1] == "is_rendering"]
    assert len(polls) >= 1
    # one preflight + one same-cycle witness per poll after a flag-less status
    assert len(globals_) >= len(polls), "global witness must ride each flag-less poll"


def test_poll_fails_typed_when_global_state_missing(tmp_path: Path) -> None:
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [_status_without_flag()]
    script[("render", "is_rendering")] = [{"success": True}]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-global-state-missing"):
        render_native(ctx, "render_native", _render_params())


def test_poll_never_completes_while_globally_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress alone never completes; a true global boolean blocks it."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [
        {"success": True, "JobStatus": "Rendering", "CompletionPercentage": 100}
    ]
    # preflight sees stopped; every poll-cycle witness sees rendering.
    script[("render", "is_rendering")] = [
        {"success": True, "rendering": False},
        {"success": True, "rendering": True},
    ]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-timeout"):
        render_native(ctx, "render_native", _render_params())


def test_wrong_dimensions(tmp_path: Path) -> None:
    script = _script_success(tmp_path)

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        # Generate 320x180 but handler expects 1920x1080
        ffmpeg = str(FFMPEG) if FFMPEG.is_file() else "ffmpeg"
        subprocess.run(
            (
                ffmpeg,
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=30:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(td / f"{name}.mp4"),
            ),
            check=False,
            capture_output=True,
            timeout=30,
        )

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-geometry-mismatch"):
        render_native(ctx, "render_native", _render_params())


def test_wrong_fps_audio(tmp_path: Path) -> None:
    script = _script_success(tmp_path)

    def on_start_24(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        ffmpeg = str(FFMPEG) if FFMPEG.is_file() else "ffmpeg"
        subprocess.run(
            (
                ffmpeg,
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x1080:rate=24:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(td / f"{name}.mp4"),
            ),
            check=False,
            capture_output=True,
            timeout=30,
        )

    transport = ScriptedTransport(script, on_start=on_start_24)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-fps-mismatch"):
        render_native(ctx, "render_native", _render_params())


# ---------------------------------------------------------------------------
# Real-episode lifecycle regressions (v44-real-01 stp-render-native failure)
# ---------------------------------------------------------------------------


def test_timeout_stops_and_deletes_orphaned_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that never reaches 100% must stop + delete the exact job so a
    timed-out render cannot keep running in Resolve and block every later
    render-lifecycle step (measured: v44-real-01 orphaned job kept the queue
    busy after the run was declared failed)."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [
        {
            "success": True,
            "JobStatus": "Rendering",
            "CompletionPercentage": 41,
            "IsRenderingInProgress": True,
        }
    ]
    script[("render", "stop")] = [_ok()]
    script[("render", "delete_job")] = [_ok()]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-timeout") as excinfo:
        render_native(ctx, "render_native", _render_params())
    stops = [c for c in transport.calls if c[1] == "stop"]
    deletes = [c for c in transport.calls if c[1] == "delete_job"]
    assert stops, "timeout must stop the timed-out job (no orphaned renders)"
    assert deletes
    assert deletes[0][2].get("job_id") == "job-native-123"
    assert "job-native-123" in str(excinfo.value)


def test_long_render_progressing_completes_beyond_static_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real-episode render outlasts any fixed probe-scale budget while it
    keeps progressing. The deadline must track the job's own progress/ETA
    (bounded), never a fixed 240s wall — otherwise a healthy multi-minute
    render is killed at 4 minutes."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    # Simulated budgets must be derived from TEST constants: the production
    # 90s ETA margin would re-arm each progressing poll ~91s out, importing
    # production timing into a unit test (never wait on production margins).
    monkeypatch.setattr(render_module, "RENDER_ETA_MARGIN_SECONDS", 0.05)
    script = _script_success(tmp_path)
    # 9 polls ≈ 0.40s: outlasts a static 0.3s deadline, but every poll
    # progresses (percent up, ETA down) so the adaptive deadline must carry
    # the job to the final 100% poll.
    script[("render", "get_job_status")] = [
        {
            "success": True,
            "JobStatus": "Rendering",
            "CompletionPercentage": pct,
            "EstimatedTimeRemainingInMs": eta,
            "IsRenderingInProgress": True,
        }
        for pct, eta in [
            (5, 700),
            (12, 650),
            (20, 600),
            (30, 550),
            (40, 500),
            (52, 460),
            (60, 380),
            (72, 300),
            (80, 200),
        ]
    ] + [
        {
            "success": True,
            "JobStatus": "Complete",
            "CompletionPercentage": 100,
            "IsRenderingInProgress": False,
        }
    ]

    def on_start(cap: dict[str, str]) -> None:
        td = Path(cap.get("target_dir", str(tmp_path)))
        name = cap.get("custom_name", "finishing-native-ep-test")
        _real_mp4(td / f"{name}.mp4")

    transport = ScriptedTransport(script, on_start=on_start)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    result = render_native(ctx, "render_native", _render_params())
    assert result["job_id"] == "job-native-123"
    assert not any(c[1] == "stop" for c in transport.calls)


def test_stalled_render_times_out_even_with_eta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen ETA/percentage is a stall: the progress-gated deadline must
    NOT keep extending for a job that reports progress signals that never
    improve."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    # The stall scripts a FROZEN ETA: the FIRST poll still "progresses" vs
    # the empty history, so it re-arms with eta*factor+margin. Without the
    # patched margin that budget is max(0.2, 0.4*2+90) ≈ 90.8s — the exact
    # cause of the verifier's 20s focused-test timeout. Simulated deadlines
    # must never inherit the production 90s margin.
    monkeypatch.setattr(render_module, "RENDER_ETA_MARGIN_SECONDS", 0.05)
    script = _script_success(tmp_path)
    script[("render", "get_job_status")] = [
        {
            "success": True,
            "JobStatus": "Rendering",
            "CompletionPercentage": 41,
            "EstimatedTimeRemainingInMs": 400,
            "IsRenderingInProgress": True,
        }
    ]
    script[("render", "stop")] = [_ok()]
    script[("render", "delete_job")] = [_ok()]

    transport = ScriptedTransport(script, on_start=lambda cap: None)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-timeout"):
        render_native(ctx, "render_native", _render_params())
    stops = [c for c in transport.calls if c[1] == "stop"]
    deletes = [c for c in transport.calls if c[1] == "delete_job"]
    assert stops, "a stalled render must be stopped (no orphan keeps the queue busy)"
    assert deletes
    assert deletes[0][2].get("job_id") == "job-native-123"


def test_preflight_refusal_names_blocking_job_ids(tmp_path: Path) -> None:
    """When the queue is busy, the typed refusal must name the blocking job
    ids — the precise measured blocker — not just 'globally in progress'."""

    script = _script_success(tmp_path)
    script[("render", "is_rendering")] = [{"success": True, "rendering": True}]
    script[("render", "list_jobs")] = [
        {
            "success": True,
            "jobs": [{"JobId": "c42fe33f-blocker", "RenderJobName": "Job 1"}],
        }
    ]

    transport = ScriptedTransport(script, on_start=lambda cap: None)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-already-in-progress") as excinfo:
        render_native(ctx, "render_native", _render_params())
    assert "c42fe33f-blocker" in str(excinfo.value)
    assert not any(c[1] == "prepare_render_job" for c in transport.calls)


def test_audio_stage_timeout_never_orphans_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared probe-render seam (loudness QC / DRX frame evidence) must
    use the SAME lifecycle: a poll timeout stops + deletes the exact job.
    Measured on v44-real-01: the orphaned loudness job kept rendering past
    the failed run and blocked every later render step."""

    import services.mcp_execution.live_handlers.render_poll as render_module  # noqa: PLC0415
    from services.mcp_execution.live_handlers.audio_render import (  # noqa: PLC0415
        render_fresh_audio,
    )

    monkeypatch.setattr(render_module, "RENDER_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(render_module, "RENDER_POLL_SECONDS", 0.05)
    script: dict[tuple[str, str], list[dict[str, object]]] = {
        ("render", "prepare_render_job"): [
            {"success": True, "job_id": "job-t5-orphan"}
        ],
        ("render", "start"): [_ok()],
        ("render", "get_job_status"): [
            {
                "success": True,
                "JobStatus": "Rendering",
                "CompletionPercentage": 41,
                "IsRenderingInProgress": True,
            }
        ],
        ("render", "stop"): [_ok()],
        ("render", "delete_job"): [_ok()],
    }
    transport = ScriptedTransport(script)
    ctx = LiveSessionContext.build(transport, {}, render_dir=str(tmp_path))
    with pytest.raises(LiveAdapterError, match="render-timeout"):
        render_fresh_audio(ctx, "audio-loudness_peak_qc-test")
    stops = [c for c in transport.calls if c[1] == "stop"]
    deletes = [c for c in transport.calls if c[1] == "delete_job"]
    assert stops, "audio-stage poll timeout must stop its job"
    assert deletes
    assert deletes[0][2].get("job_id") == "job-t5-orphan"


def test_production_deadline_arithmetic_suits_multi_minute_renders() -> None:
    """Pin the PRODUCTION poll constants: a healthy render's re-armed budget
    must exceed twice its own reported ETA (a multi-minute render is never
    killed by the fixed 240s floor), while the ceiling bounds episode scale.
    Tests patch these constants; this pin forces any production change to be
    conscious rather than accidental."""
    from services.mcp_execution.live_handlers import render_poll  # noqa: PLC0415

    assert render_poll.RENDER_TIMEOUT_SECONDS == 240.0
    assert render_poll.RENDER_TIMEOUT_CEILING_SECONDS == 7200.0
    assert render_poll.RENDER_ETA_FACTOR == 2.0
    assert render_poll.RENDER_ETA_MARGIN_SECONDS == 90.0
    for eta_s in (60.0, 240.0, 300.0, 1800.0):
        budget = max(
            render_poll.RENDER_TIMEOUT_SECONDS,
            eta_s * render_poll.RENDER_ETA_FACTOR + render_poll.RENDER_ETA_MARGIN_SECONDS,
        )
        assert budget >= 2 * eta_s, "a healthy render must outlive its own ETA by >= 2x"
        assert budget <= render_poll.RENDER_TIMEOUT_CEILING_SECONDS


# ---- Task 8 repair: trusted media EOF facts (ffprobe nb_frames) -----------

def _patch_probe_payload(monkeypatch, payload: object) -> None:
    monkeypatch.setattr(nrm, "_ffprobe_payload", lambda probe_bin, media: payload)
    monkeypatch.setattr(nrp, "_ffprobe_payload", lambda probe_bin, media: payload)


def test_probe_video_frame_count_reads_nb_frames(monkeypatch) -> None:
    _patch_probe_payload(
        monkeypatch,
        {"streams": [{"codec_type": "video", "nb_frames": "8467"}]},
    )
    assert probe_video_frame_count(Path("m.mov"), Path("ffprobe")) == 8467


def test_probe_video_frame_count_refused_when_nb_frames_missing(monkeypatch) -> None:
    _patch_probe_payload(monkeypatch, {"streams": [{"codec_type": "video"}]})
    with pytest.raises(LiveAdapterError) as exc:
        probe_video_frame_count(Path("m.mov"), Path("ffprobe"))
    assert exc.value.code == "media-frame-count-missing"


def test_probe_video_frame_count_refused_when_non_positive(monkeypatch) -> None:
    _patch_probe_payload(
        monkeypatch,
        {"streams": [{"codec_type": "video", "nb_frames": "0"}]},
    )
    with pytest.raises(LiveAdapterError) as exc:
        probe_video_frame_count(Path("m.mov"), Path("ffprobe"))
    assert exc.value.code == "media-frame-count-invalid"


def test_probe_video_frame_count_refused_without_video_stream(monkeypatch) -> None:
    _patch_probe_payload(monkeypatch, {"streams": []})
    with pytest.raises(LiveAdapterError) as exc:
        probe_video_frame_count(Path("m.mov"), Path("ffprobe"))
    assert exc.value.code == "media-frame-count-missing"
