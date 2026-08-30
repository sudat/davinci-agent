"""Live capability probes + parity + flag transitions (task 11, Gate V43-0).

Marked ``mcp_live``: runs ONLY under ``-m mcp_live`` with the pinned server
and a live Resolve.  Drives the 22 capability probes of
``capabilities/v4.3/mcp-fit.json`` THROUGH the typed ops surface, records
per-probe evidence logs + the call ledger, fills the matrix, emits the
``mcp-capability-snapshot-v1`` artifact, then runs the base-cut parity
(legacy vs mcp) and the execution_backend flag transitions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from typing import Final

import pytest

from services.config.backends import load_backends, set_backend
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.execution_runner import BackendPolicyError, McpExecutionRunner
from services.qa.parity_harness import run_parity
from services.toolchain.mcp_fit import EXPECTED_ROW_COUNT, load_mcp_fit
from tests.mcp_client.live_support import (
    MCP_FIT_PATH,
    PROBES_DIR,
    VIDEO_PIPELINE_ROOT,
    LiveSession,
    close_live_session,
    deep_shots_from_payload,
    open_live_session,
    run_probe,
    standard_visual_from_manifest,
    write_matrix_and_snapshot,
)

PIN_PATH: Final = VIDEO_PIPELINE_ROOT / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
VENDOR_DRX: Final = (
    VIDEO_PIPELINE_ROOT.parent
    / "private/vendor/davinci-resolve-mcp/resolve-advanced/test/fixtures/node-color-blue.drx"
)
PLACE_TL: Final = "v43-probe-place-tl"
EXPECTED_PLACEMENTS: Final = (
    ("parity-src-001", 10, 70, 0),
    ("parity-src-002", 0, 60, 60),
    ("parity-src-003", 20, 50, 120),
)


def _mcp_live_selected(pytestconfig: pytest.Config) -> bool:
    return "mcp_live" in (pytestconfig.getoption("markexpr") or "")


def _stem(name: str | None) -> str:
    if name is None:
        return ""
    return name.rsplit(".", 1)[0] if "." in name else name


@pytest.fixture(scope="module")
def live(pytestconfig: pytest.Config) -> Generator[LiveSession, None, None]:
    if not _mcp_live_selected(pytestconfig):
        pytest.skip("live probes require -m mcp_live")
    media_dir = Path(tempfile.mkdtemp(prefix="v43-probes-media-"))
    session = open_live_session(media_dir, f"v43-probe-{time.strftime('%H%M%S')}")
    try:
        yield session
    finally:
        close_live_session(session)
        shutil.rmtree(media_dir, ignore_errors=True)


ProbeFn = Callable[[LiveSession, dict[str, str]], tuple[str, str]]


# ---------------------------------------------------------------------------
# probe implementations
# ---------------------------------------------------------------------------


def _probe_project_timeline_creation(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    project = s.ops.prepare_project(s.project_name, 30)
    if not project.ok:
        return s.fail(project, "project_manager create + timelineFrameRate=30 (fresh project)")
    loaded = s.ops.get_current_project()
    if loaded.name != s.project_name:
        s.ops.load_project(s.project_name)
        loaded = s.ops.get_current_project()
    if loaded.name != s.project_name:
        return "failed", f"current project is {loaded.name!r}, expected {s.project_name!r}"
    timeline = s.ops.ensure_timeline("v43-probe-tl-01")
    if not timeline.ok:
        return s.fail(timeline, "empty timeline create + set current")
    current = s.ops.get_current_timeline()
    if current.name != "v43-probe-tl-01":
        return "failed", f"current timeline is {current.name!r}"
    readback = (
        f"project {loaded.name!r} created+current with timelineFrameRate=30 set as a "
        f"string on the fresh project (verified live: SetSetting is refused once any "
        f"timeline exists); empty timeline {current.name!r} id={current.id} created+current"
    )
    return "accepted", readback


def _probe_import_media(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    paths = [str(path) for path in s.media.values()]
    imported = s.ops.safe_import_media(paths)
    if not imported.ok or imported.imported != len(paths):
        return s.fail(imported, "media_pool safe_import_media")
    for clip in imported.clips:
        source_id = clip.name.rsplit(".", 1)[0]
        state[f"clip:{source_id}"] = clip.clip_id
    missing = sorted(set(s.media) - {k.split(":", 1)[1] for k in state if k.startswith("clip:")})
    if missing:
        return "failed", f"imported clip names do not cover sources: {missing}"
    readback = (
        f"safe_import_media imported={imported.imported} clips="
        f"{sorted(clip.name for clip in imported.clips)} with media-pool ids"
    )
    return "accepted", readback


def _placement_clip_infos(state: dict[str, str]) -> list[dict[str, object]]:
    infos: list[dict[str, object]] = []
    for source_id, start, end, record in EXPECTED_PLACEMENTS:
        base = {
            "clip_id": state[f"clip:{source_id}"],
            "start_frame": start,
            "end_frame": end,
            "record_frame": 108000 + record,
            "record_frame_mode": "absolute",
            "track_index": 1,
        }
        infos.append({**base, "media_type": 1})
        infos.append({**base, "media_type": 2})
    return infos


def _probe_exact_source_range_placement(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    ready = s.ops.ensure_timeline(PLACE_TL)
    if not ready.ok:
        return s.fail(ready, "placement timeline create + set current")
    appended = s.ops.append_to_timeline(_placement_clip_infos(state))
    if not appended.ok:
        return s.fail(appended, "media_pool append_to_timeline")
    snapshot = s.ops.timeline_structure()
    if not snapshot.ok:
        return s.fail(snapshot, "probe_timeline_structure")
    video = snapshot.tracks["video"].tracks[0].items
    if len(video) != len(EXPECTED_PLACEMENTS):
        return "failed", f"video item count {len(video)} != {len(EXPECTED_PLACEMENTS)}"
    for item, (source_id, start, end, record) in zip(video, EXPECTED_PLACEMENTS, strict=True):
        problems = []
        if _stem(item.media_pool_item_name) != source_id:
            problems.append(f"name {item.media_pool_item_name!r} != {source_id!r}")
        if item.source_start != start or item.source_end != end:
            problems.append(
                f"source span ({item.source_start},{item.source_end}) != ({start},{end})"
            )
        if item.start != 108000 + record:
            problems.append(f"record start {item.start} != {108000 + record}")
        if problems:
            return "failed", f"{source_id}: " + "; ".join(problems)
    spans = ", ".join(
        f"{i.media_pool_item_name}:src[{i.source_start},{i.source_end})@{i.start}" for i in video
    )
    readback = (
        f"3 placements (video+audio twins) at exact source in/out and record frames; "
        f"video readback spans {spans}"
    )
    return "accepted", readback


def _probe_source_record_readback(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    snapshot = s.ops.timeline_structure()
    if not snapshot.ok:
        return s.fail(snapshot, "probe_timeline_structure")
    video = snapshot.tracks["video"].tracks[0].items
    audio = snapshot.tracks["audio"].tracks[0].items
    if len(video) != 3 or len(audio) != 3:
        return "failed", f"track item counts video={len(video)} audio={len(audio)}"
    for v, a in zip(video, audio, strict=True):
        if v.start != a.start or v.end != a.end:
            return "failed", f"av link drift: video@{v.start} audio@{a.start}"
        if v.source_start != a.source_start or v.source_end != a.source_end:
            detail = (
                f"av source drift at {v.start}: "
                f"v=({v.source_start},{v.source_end}) a=({a.source_start},{a.source_end})"
            )
            return "failed", detail
    report = s.ops.source_range_report()
    if not report.ok or not report.ranges:
        return s.fail(report, "source_range_report")
    readback = (
        f"structure+source_range_report: video/audio record and source spans aligned; "
        f"timeline [{snapshot.start_frame},{snapshot.end_frame}) @{snapshot.start_timecode}; "
        f"{len(report.ranges)} source ranges reported"
    )
    return "accepted", readback


def _probe_subtitle_capability(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    probe = s.ops.subtitle_generation_probe()
    if not probe.ok:
        return s.fail(probe, "subtitle_generation_probe")
    settings = (
        json.dumps(probe.settings, sort_keys=True)[:200] if probe.settings else None
    )
    readback = (
        f"subtitle_generation_probe would_generate={probe.would_generate}; "
        f"settings={settings} "
        "(probe-level: synthetic media carries no speech, generation itself not exercised)"
    )
    return "accepted", readback


def _probe_title_text_plus(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.ensure_timeline("v43-probe-title-tl")
    inserted = None
    used_name = ""
    for name in ("Text+", "Fusion Title", "Title"):
        inserted = s.ops.insert_fusion_title(name)
        if inserted.ok:
            used_name = name
            break
    if inserted is None or not inserted.ok:
        return s.fail(inserted, "timeline insert_fusion_title")
    text = s.ops.set_title_text("V43 PROBE TITLE", track_index=1, item_index=0)
    if not text.ok:
        return s.fail(text, "timeline set_title_text")
    comp = s.ops.fusion_comp_count(track_index=1, item_index=0)
    if not comp.ok or not comp.count or comp.count < 1:
        return s.fail(comp, "timeline_item_fusion get_comp_count")
    readback = (
        f"insert_fusion_title({used_name!r}) + set_title_text(ok={text.success}) + "
        f"fusion comp count={comp.count} on title item"
    )
    return "accepted", readback


def _probe_fusion_template_insertion(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.ensure_timeline("v43-probe-fusion-tl")
    inserted = s.ops.insert_fusion_composition()
    if not inserted.ok:
        return s.fail(inserted, "timeline insert_fusion_composition")
    comp = s.ops.fusion_comp_count(track_index=1, item_index=0)
    if not comp.ok or not comp.count or comp.count < 1:
        return s.fail(comp, "timeline_item_fusion get_comp_count")
    return "accepted", f"insert_fusion_composition ok; item fusion comp count={comp.count}"


def _probe_clip_transform_punch_in(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.set_current_timeline(PLACE_TL)
    zoom = {"ZoomX": 1.25, "ZoomY": 1.25, "Pan": 0.05}
    written = s.ops.set_transform(zoom, track_index=1, item_index=0)
    if not written.ok:
        return s.fail(written, "timeline_item set_transform")
    readback = s.ops.get_transform(track_index=1, item_index=0)
    if readback.ZoomX is None or abs(readback.ZoomX - 1.25) > 0.01:
        return "failed", f"ZoomX readback {readback.ZoomX} != 1.25"
    summary = (
        f"punch-in set ZoomX/ZoomY=1.25 Pan=0.05 -> readback "
        f"ZoomX={readback.ZoomX} ZoomY={readback.ZoomY} Pan={readback.Pan}"
    )
    return "accepted", summary


def _probe_transition_path(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    caps = s.ops.edit_kernel_capabilities()
    unsupported = caps.unsupported or {}
    cloning = unsupported.get("transition_cloning", "not reported")
    duplication = s.ops.duplicate_clips(selected=True, copy_properties=["transitions"])
    summary = (
        f"public API transition gap confirmed: edit_kernel_capabilities.unsupported."
        f"transition_cloning={cloning!r}; duplicate_clips(copy_properties=['transitions']) "
        f"ok={duplication.ok} - no MCP path to ADD transitions; fallback template_external"
    )
    return "failed", summary


def _probe_audio_property_operation(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.set_current_timeline(PLACE_TL)
    result = s.ops.set_audio_properties({"Volume": -6.0}, track_index=1, item_index=0)
    if not result.ok:
        return s.fail(result, "timeline safe_set_audio_properties")
    row = result.results.get("Volume")
    if row is None or not row.write:
        return "failed", f"Volume row did not write: {row}"
    readback = (
        f"audio item Volume -6.0: write={row.write} readback={row.readback} "
        f"original={row.original} (auto-restored)"
    )
    return "accepted", readback


def _probe_voice_isolation(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    caps = s.ops.voice_isolation_capabilities(track_index=1)
    enabled = s.ops.set_voice_isolation_state({"isEnabled": True, "amount": 60}, track_index=1)
    if not enabled.ok:
        return s.fail(enabled, "timeline set_voice_isolation_state")
    state = s.ops.get_voice_isolation_state(track_index=1)
    restored = s.ops.set_voice_isolation_state({"isEnabled": False, "amount": 0}, track_index=1)
    if state.is_enabled is not True:
        return "failed", f"voice isolation state readback isEnabled={state.is_enabled}"
    readback = (
        f"track voice isolation set(isEnabled=true,amount=60) -> readback "
        f"isEnabled={state.is_enabled} amount={state.amount}; restored ok={restored.ok}; "
        f"caps item keys={sorted((caps.item or {}).keys())[:6]}"
    )
    return "accepted", readback


def _probe_bgm_track_ducking(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    caps = s.ops.audio_mix_capability_report()
    report_text = json.dumps(
        {
            "capabilities": caps.capabilities,
            "supported": caps.supported,
            "partial": caps.partially_supported,
        },
        sort_keys=True,
    )
    duck_rows = [row for row in report_text.lower().split(",") if "duck" in row]
    if duck_rows:
        return "accepted", f"audio_mix_capability_report ducking surface: {duck_rows[:3]}"
    summary = (
        "no ducking surface anywhere in timeline audio_mix_capability_report "
        f"(top-level keys present: capabilities={bool(caps.capabilities)} "
        f"supported={sorted((caps.supported or {}).keys())[:6]}); Resolve 21.0.4 "
        "scripting API exposes no Fairlight ducking control - fallback legacy_direct"
    )
    return "failed", summary


def _probe_color_grade_preset_drx(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.set_current_timeline(PLACE_TL)
    temp_dir = Path(tempfile.mkdtemp(prefix="v43-drx-"))
    drx_copy = temp_dir / "node-color-blue.drx"
    shutil.copyfile(VENDOR_DRX, drx_copy)
    dry = s.ops.safe_apply_drx(str(drx_copy), dry_run=True)
    if not dry.ok:
        return s.fail(dry, "timeline_item_color safe_apply_drx (dry_run)")
    applied = s.ops.safe_apply_drx(str(drx_copy))
    if applied.confirm_token:
        applied = s.ops.safe_apply_drx(str(drx_copy), confirm_token=applied.confirm_token)
    if not applied.ok:
        return s.fail(applied, "timeline_item_color safe_apply_drx (apply)")
    shutil.rmtree(temp_dir, ignore_errors=True)
    readback = (
        "safe_apply_drx dry_run ok + graph.ApplyGradeFromDRX applied vendor "
        "node-color-blue.drx (copied to system temp) to video item 1"
    )
    return "accepted", readback


def _probe_render_configuration(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    fmt = s.ops.render_set_format_and_codec("mp4", "h264")
    if not fmt.ok:
        return s.fail(fmt, "render set_format_and_codec")
    settings = s.ops.render_set_settings(
        {
            "FormatWidth": 1920,
            "FormatHeight": 1080,
            "FrameRate": 30.0,
            "AudioCodec": "aac",
            "AudioSampleRate": 48000,
            "TargetDir": tempfile.gettempdir(),
            "CustomName": "v43-probe-render",
        }
    )
    if not settings.ok:
        return s.fail(settings, "render set_settings")
    validated = s.ops.validate_render_settings(
        {
            "FormatWidth": 1920,
            "FormatHeight": 1080,
            "FrameRate": 30.0,
            "AudioCodec": "aac",
            "AudioSampleRate": 48000,
        }
    )
    current = s.ops.render_get_format_and_codec()
    if validated.valid is not True:
        return "failed", f"validate_render_settings rejected: {validated.errors}"
    summary = (
        f"set+validated settings echo {json.dumps(validated.settings, sort_keys=True)}; "
        f"GetRenderSettings is unavailable on this build (settings read back via the "
        f"validated echo); format/codec readback={current.format}/{current.codec}"
    )
    return "accepted", summary


def _probe_render_job_lifecycle(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    added = s.ops.render_add_job()
    if added.job_id is None:
        return s.fail(added, "render add_job")
    job_id = added.job_id
    started = s.ops.render_start([job_id])
    if not started.ok:
        s.ops.render_delete_job(added.job_id)
        return s.fail(started, "render start")
    deadline = time.monotonic() + 180.0
    status = None
    while time.monotonic() < deadline:
        status = s.ops.render_job_status(job_id)
        if status.completion_percentage is not None and status.completion_percentage >= 100:
            break
        time.sleep(2.0)
    jobs = s.ops.render_list_jobs()
    deleted = s.ops.render_delete_job(job_id)
    if status is None or (status.completion_percentage or 0) < 100:
        pct = getattr(status, "completion_percentage", None)
        job_status = getattr(status, "job_status", None)
        detail = (
            f"job {job_id} add->start ok but completion={pct} "
            f"(JobStatus={job_status!r}); cleanup delete ok={deleted.ok}"
        )
        return "partial", detail
    summary = (
        f"job {job_id}: add -> start -> CompletionPercentage=100 "
        f"(JobStatus={status.job_status!r}) -> list({len(jobs.jobs)} jobs) "
        f"-> delete ok={deleted.ok}"
    )
    return "accepted", summary


def _probe_gaps_overlaps_missing_media(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    infos: list[dict[str, object]] = []
    for source_id, record in (("parity-src-001", 0), ("parity-src-002", 120)):
        infos.append(
            {
                "clip_id": state[f"clip:{source_id}"],
                "start_frame": 0,
                "end_frame": 60,
                "record_frame": 108000 + record,
                "record_frame_mode": "absolute",
                "track_index": 1,
                "media_type": 1,
            }
        )
    setup_outcomes = (
        (s.ops.ensure_timeline("v43-probe-gaps-tl"), "gaps timeline setup"),
        (s.ops.append_to_timeline(infos), "gaps timeline append_to_timeline"),
    )
    for outcome, what in setup_outcomes:
        if not outcome.ok:
            return s.fail(outcome, what)
    gaps = s.ops.detect_gaps_overlaps()
    if not gaps.ok:
        return s.fail(gaps, "timeline detect_gaps_overlaps")
    if len(gaps.gaps) < 1:
        return "failed", f"expected >=1 detected gap, got {len(gaps.gaps)}"
    missing_before = s.ops.detect_missing_media()
    if not missing_before.ok or missing_before.missing_count != 0:
        return s.fail(missing_before, "timeline detect_missing_media (baseline)")
    hidden = Path(s.media["parity-src-002"]).with_suffix(".hidden")
    shutil.move(str(s.media["parity-src-002"]), hidden)
    try:
        missing_after = s.ops.detect_missing_media()
    finally:
        shutil.move(str(hidden), str(s.media["parity-src-002"]))
    if not missing_after.ok or not missing_after.missing_count:
        return "failed", f"missing media not detected after rename: {missing_after.missing_count}"
    first_gap = gaps.gaps[0]
    readback = (
        f"detect_gaps_overlaps found {len(gaps.gaps)} gap(s) "
        f"(first: {first_gap.track_type}@[{first_gap.start},{first_gap.end})); "
        f"detect_missing_media 0 -> {missing_after.missing_count} after source rename (restored)"
    )
    return "accepted", readback


def _probe_conform_source_ranges(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    s.ops.set_current_timeline(PLACE_TL)
    report = s.ops.source_range_report()
    if not report.ok or not report.occurrences:
        return s.fail(report, "timeline source_range_report")
    placed = sorted(
        tuple(row.source_range or ())
        for row in report.occurrences
        if row.source_range
    )
    expected = sorted(
        [(start, end) for _, start, end, _ in EXPECTED_PLACEMENTS for _ in (0, 1)]
    )
    if placed != expected:
        return "failed", f"source ranges {placed} != expected {expected}"
    readback = (
        f"source_range_report occurrences ({len(placed)} rows incl. audio twins) cover "
        "every placed source in/out frame span exactly"
    )
    return "accepted", readback


def _probe_analysis_standard_pass(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    clip_id = state["clip:parity-src-001"]
    plan = s.ops.analyze_clip(clip_id, dry_run=True)
    if not plan.ok:
        return s.fail(plan, "media_analysis analyze_clip (dry_run plan)")
    executed = s.ops.analyze_clip(clip_id, dry_run=False, verbose=True)
    if not executed.ok:
        return s.fail(executed, "media_analysis analyze_clip (executed)")
    manifest = executed.manifest
    if executed.status != "pending_host_vision_analysis" or not manifest:
        return "partial", f"executed pass status={executed.status!r} (no pending vision payload)"
    visual = standard_visual_from_manifest(manifest)
    if visual is None:
        return "partial", "pending payload carried no shot_table to author visual from"
    committed = s.ops.commit_vision(visual, clip_id=clip_id)
    if not committed.ok or not committed.visual_json:
        return s.fail(committed, "media_analysis commit_vision")
    state["analysis:visual_json"] = committed.visual_json
    readback = (
        f"analyze_clip plan ok; executed pass -> pending_host_vision_analysis with real "
        f"shot_table; commit_vision stored {Path(committed.visual_json).name}"
    )
    return "accepted", readback


def _probe_deep_shot_analysis(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    clip_id = state.get("clip:parity-src-001", "")
    if not clip_id:
        return "failed", "no analyzed clip available (standard pass must run first)"
    estimate = s.ops.deepen(clip_id)
    if not estimate.ok:
        return s.fail(estimate, "media_analysis deepen (estimate)")
    confirmed = s.ops.deepen(clip_id, confirm_token=estimate.confirm_token)
    if not confirmed.ok:
        return s.fail(confirmed, "media_analysis deepen (confirmed payload)")
    payload_shots = [row for row in confirmed.shot_table if isinstance(row, dict)]
    if not payload_shots:
        return "partial", "deepen confirmed but carried no shot rows"
    shots = deep_shots_from_payload(payload_shots)
    committed = s.ops.commit_shot_vision(
        shots, clip_id=clip_id, vision_token=confirmed.vision_token
    )
    if not committed.ok:
        return s.fail(committed, "media_analysis commit_shot_vision")
    readback = (
        f"deepen estimate -> confirm (real shot rows={len(payload_shots)}) -> "
        f"commit_shot_vision ok (updated={committed.updated})"
    )
    return "accepted", readback


def _probe_edit_engine_selects(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    planned = s.ops.edit_engine_plan_selects(PLACE_TL)
    if not planned.ok:
        return s.fail(planned, "edit_engine plan_selects")
    if not planned.plan_id:
        return (
            "partial",
            "plan_selects ok but produced no plan_id (probe shots carry no select metadata)",
        )
    executed = s.ops.edit_engine_execute_selects(planned.plan_id)
    if executed.confirm_token:
        executed = s.ops.edit_engine_execute_selects(
            planned.plan_id, confirm_token=executed.confirm_token
        )
    if not executed.ok:
        return s.fail(executed, "edit_engine execute_selects")
    readback = (
        f"plan_selects({PLACE_TL}) -> plan {planned.plan_id} -> "
        f"execute_selects created selects timeline"
    )
    return "accepted", readback


def _probe_alternate_shot_similarity(s: LiveSession, state: dict[str, str]) -> tuple[str, str]:
    clip_id = state.get("clip:parity-src-001", "")
    built = s.ops.build_embeddings(clip_id=clip_id)
    if not built.ok:
        return s.fail(built, "media_analysis build_embeddings")
    similar = s.ops.find_similar(text="test pattern calibration", clip_id=clip_id)
    if not similar.ok:
        return s.fail(similar, "media_analysis find_similar")
    rows = similar.results or similar.matches
    top = f" top={json.dumps(rows[0], sort_keys=True)[:160]}" if rows else " (empty index)"
    return "accepted", f"build_embeddings ok; find_similar(text) returned {len(rows)} row(s){top}"


def _probe_advanced_delivery_qc(s: LiveSession, _state: dict[str, str]) -> tuple[str, str]:
    boundary = s.ops.render_boundary_report()
    if not boundary.ok:
        return s.fail(boundary, "render export_render_boundary_report")
    validated = s.ops.validate_render_settings(
        {"FormatWidth": 1920, "FormatHeight": 1080, "FrameRate": 30.0}
    )
    if not validated.ok or validated.valid is not True:
        return s.fail(validated, "render validate_render_settings")
    capability_keys = sorted((boundary.capabilities or {}).keys())
    readback = (
        f"export_render_boundary_report capabilities={capability_keys} + "
        f"validate_render_settings ok - delivery QC surface reachable"
    )
    return "accepted", readback


PROBES: Final = (
    ("project-timeline-creation", _probe_project_timeline_creation),
    ("import-media", _probe_import_media),
    ("exact-source-range-placement", _probe_exact_source_range_placement),
    ("source-record-readback", _probe_source_record_readback),
    ("subtitle-capability", _probe_subtitle_capability),
    ("title-text-plus", _probe_title_text_plus),
    ("fusion-template-insertion", _probe_fusion_template_insertion),
    ("clip-transform-punch-in", _probe_clip_transform_punch_in),
    ("transition-path", _probe_transition_path),
    ("audio-property-operation", _probe_audio_property_operation),
    ("voice-isolation", _probe_voice_isolation),
    ("bgm-track-ducking", _probe_bgm_track_ducking),
    ("color-grade-preset-drx", _probe_color_grade_preset_drx),
    ("render-configuration", _probe_render_configuration),
    ("render-job-lifecycle", _probe_render_job_lifecycle),
    ("gaps-overlaps-missing-media", _probe_gaps_overlaps_missing_media),
    ("conform-source-ranges", _probe_conform_source_ranges),
    ("analysis-standard-pass", _probe_analysis_standard_pass),
    ("deep-shot-analysis", _probe_deep_shot_analysis),
    ("edit-engine-selects", _probe_edit_engine_selects),
    ("alternate-shot-similarity", _probe_alternate_shot_similarity),
    ("advanced-delivery-qc", _probe_advanced_delivery_qc),
)


@pytest.mark.mcp_live
def test_live_capability_probes_fill_matrix_and_snapshot(
    live: LiveSession, pytestconfig: pytest.Config
) -> None:
    if not _mcp_live_selected(pytestconfig):
        pytest.skip("live probes require -m mcp_live")
    state: dict[str, str] = {}
    logs = [run_probe(live, capability, fn, state) for capability, fn in PROBES]
    assert len(logs) == EXPECTED_ROW_COUNT
    snapshot_path = write_matrix_and_snapshot(
        logs,
        provider_version="2.98.3",
        pin_commit=json.loads(PIN_PATH.read_bytes())["commit"],
        server_mode="compound",
        resolve_build=live.resolve_version_string,
    )
    # Misleading-success guard: statuses parsed from the persisted matrix, not memory.
    matrix = load_mcp_fit(MCP_FIT_PATH)
    statuses = {row["status"] for row in matrix["capabilities"]}
    assert statuses <= {"accepted", "failed", "partial"}
    assert all(row["evidence_refs"] for row in matrix["capabilities"])
    assert all(row["readback"] for row in matrix["capabilities"])
    assert snapshot_path.is_file()
    (PROBES_DIR / "summary.json").write_bytes(
        json.dumps(logs, sort_keys=True, indent=2).encode() + b"\n"
    )


@pytest.mark.mcp_live
def test_live_basecut_parity_mcp_vs_legacy(live: LiveSession, pytestconfig: pytest.Config) -> None:
    if not _mcp_live_selected(pytestconfig):
        pytest.skip("live parity requires -m mcp_live")
    report = run_parity(
        VIDEO_PIPELINE_ROOT / "tests/qa/fixtures/parity-basecut/ir.json",
        backend_a="legacy",
        backend_b="mcp",
        output_path=PROBES_DIR / "parity-basecut-report.json",
    )
    assert report["differences"] == [], json.dumps(report["differences"], indent=2)[:2000]


def _backend_smoke(backends_path: Path, tmp_dir: Path) -> dict[str, object]:
    """Prove which execution path the REAL flag file selects.

    A lease-holding McpExecutionRunner with a recording fake transport
    attempts one mutating call: under ``mcp`` the call passes the BACKEND
    guard and reaches the transport; under ``legacy_direct`` it is refused
    with ``BackendPolicyError`` — the flag is load-bearing, not cosmetic.
    """
    store = StateStore.open(tmp_dir / "smoke-state.sqlite3")
    job_id, stage, holder = "dev-flag-smoke", "stage-a", "holder-1"
    store.acquire_lease(
        resource=stage_resource(job_id, stage), holder=holder, now=1000, ttl_seconds=60
    )
    transport_calls: list[tuple[str, str, object]] = []

    def fake_transport(
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        transport_calls.append((tool_name, action, normalized_params))
        return {"ok": True}

    runner = McpExecutionRunner(
        store=store,
        job_id=job_id,
        stage_name=stage,
        holder_token=holder,
        ledger_dir=tmp_dir / "smoke-ledger",
        clock=SequenceClock(1000),
        transport=fake_transport,
        backends_path=backends_path,
    )
    refused: str | None = None
    try:
        runner.execute(tool_name="timeline", action="set_name", normalized_params={})
    except BackendPolicyError as exc:
        refused = exc.code
    return {
        "flag": load_backends(backends_path).execution_backend,
        "mutating_call_reached_transport": bool(transport_calls),
        "refusal_code": refused,
    }


@pytest.mark.mcp_live
def test_execution_backend_flag_transitions(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    if not _mcp_live_selected(pytestconfig):
        pytest.skip("flag transitions require -m mcp_live (recorded after parity acceptance)")
    backends_path = VIDEO_PIPELINE_ROOT / "config" / "backends.json"
    transitions: list[dict[str, object]] = []
    try:
        # 1. mcp (post-parity production state)
        first = set_backend("execution_backend", "mcp")
        smoke_mcp = _backend_smoke(backends_path, tmp_path / "mcp")
        assert smoke_mcp["flag"] == "mcp"
        assert smoke_mcp["mutating_call_reached_transport"] is True
        row = {
            "transition": "legacy_direct->mcp",
            "config": first.model_dump(mode="json"),
            "smoke": smoke_mcp,
        }
        transitions.append(row)
        # 2. rollback to legacy_direct — mutating calls must be refused
        rolled = set_backend("execution_backend", "legacy_direct")
        smoke_legacy = _backend_smoke(backends_path, tmp_path / "legacy")
        assert smoke_legacy["flag"] == "legacy_direct"
        assert smoke_legacy["refusal_code"] == "backend-policy"
        row = {
            "transition": "mcp->legacy_direct",
            "config": rolled.model_dump(mode="json"),
            "smoke": smoke_legacy,
        }
        transitions.append(row)
        # 3. final production state: mcp again
        final = set_backend("execution_backend", "mcp")
        smoke_final = _backend_smoke(backends_path, tmp_path / "final")
        assert smoke_final["mutating_call_reached_transport"] is True
        row = {
            "transition": "legacy_direct->mcp(final)",
            "config": final.model_dump(mode="json"),
            "smoke": smoke_final,
        }
        transitions.append(row)
    finally:
        if load_backends(backends_path).execution_backend != "mcp":
            set_backend("execution_backend", "mcp")
    transitions_payload = {
        "transitions": transitions,
        "final_config": load_backends(backends_path).model_dump(mode="json"),
    }
    (PROBES_DIR / "backend-transitions.json").write_bytes(
        json.dumps(transitions_payload, sort_keys=True, indent=2).encode() + b"\n"
    )


@pytest.mark.mcp_live
def test_episode0_freeze_manifest_live(pytestconfig: pytest.Config, tmp_path: Path) -> None:
    if not _mcp_live_selected(pytestconfig):
        pytest.skip("episode0 live check requires -m mcp_live")
    episode_json = (
        VIDEO_PIPELINE_ROOT.parent / "private/reference-episodes/real-01/episode.json"
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "services.cli.episode0", "freeze-manifest",
            "--episode-json", str(episode_json),
            "--out", str(tmp_path / "manifest.json"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(VIDEO_PIPELINE_ROOT),
        check=False,
    )
    note = {
        "command": "uv run python -m services.cli.episode0 freeze-manifest (real-01)",
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-400:],
        "stderr_tail": result.stderr[-400:],
        "limitation": (
            "A full v1 editorial chain on the DJI clip is not runnable live: the clip has no "
            "transcript (no speech pipeline output available in this environment), so editorial "
            "stages beyond ingest/manifest cannot execute. The legacy-vs-mcp base-cut parity run "
            "(parity-basecut-report.json) is the base-execution-path demonstration for Gate V43-0."
        ),
    }
    (PROBES_DIR / "episode0-freeze-manifest.json").write_bytes(
        json.dumps(note, sort_keys=True, indent=2).encode() + b"\n"
    )
    assert result.returncode == 0, result.stderr[-800:]
