# noqa: INP001 (evidence tree is not an importable package by design)
"""The fixed-order disposable session flow, decomposed into phase functions.

Phase order (each runs only if no earlier phase failed): prepare project
(refuses pre-existing names) → fps → timeline → import → audio placement →
settings echo probes → ONE 900 s-bounded generation → readback. The caller
MUST run ``probe_seam.dispose_safely`` in a ``finally``."""

from __future__ import annotations

import time

import caption_logic as logic
from probe_seam import (
    FPS,
    GENERATE_TIMEOUT_S,
    IMPORT_TIMEOUT_S,
    RUN_LABELS,
    TIMELINE_NAME,
    ActionSeam,
    FlowState,
    MediaInput,
    Sink,
    classify_exception,
    ok_payload,
)


def _prepare_project(state: FlowState) -> None:
    name = f"probe-resolve-autocap-{state.run_label}"
    created = state.call("project_manager", "create", {"name": name})
    state.phase("project_create")
    if not ok_payload(created):
        state.fail("error", "project_create")
        return
    set_fps = state.call("project_settings", "set_setting",
                         {"name": "timelineFrameRate", "value": str(FPS)})
    state.phase("project_fps")
    if not ok_payload(set_fps):
        state.fail("error", "project_fps")


def _ensure_timeline(state: FlowState) -> None:
    made = state.call("media_pool", "create_timeline", {"name": TIMELINE_NAME})
    if not ok_payload(made):
        state.fail("error", "timeline_create")
        return
    state.call("timeline", "set_current", {"name": TIMELINE_NAME})
    current = state.call("timeline", "get_current", {})
    state.phase("timeline_current")
    start = current.get("start_frame")
    if not ok_payload(current) or not isinstance(start, int) or isinstance(start, bool):
        state.fail("error", "timeline_current")
        return
    state.timeline_start = start
    state.report["timeline_start_frame"] = start


def _import_media(state: FlowState) -> None:
    imported = state.call("media_pool", "safe_import_media", {"paths": [state.media.path]},
                          timeout_seconds=IMPORT_TIMEOUT_S)
    state.phase("import")
    clips = imported.get("clips")
    first = clips[0] if isinstance(clips, list) and clips else None
    if not ok_payload(imported) or not isinstance(first, dict):
        state.fail("error", "import")
        return
    state.clip_id = str(first.get("id", ""))
    props = state.call("media_pool_item", "get_clip_property",
                       {"clip_id": state.clip_id, "key": ["FPS", "Duration"]})
    state.phase("clip_props")
    properties = props.get("properties")
    if ok_payload(props) and isinstance(properties, dict):
        try:
            state.source_frames = logic.parse_duration_frames(
                properties.get("Duration"), properties.get("FPS"))
        except logic.ProbeRefusalError:
            state.fail("error", "clip_props")
    else:
        state.fail("error", "clip_props")


def _place_audio(state: FlowState) -> None:
    count_before = state.call("timeline", "get_track_count", {"track_type": "audio"}).get("count")
    state.phase("tracks_before")
    appended = state.call("media_pool", "append_to_timeline", {"clip_infos": [{
        "clip_id": state.clip_id, "media_type": 2, "start_frame": 0,
        "end_frame": state.source_frames, "record_frame": state.timeline_start,
        "record_frame_mode": "absolute", "track_index": 1,
    }]}, timeout_seconds=IMPORT_TIMEOUT_S)
    items = state.call("timeline", "get_items_in_track",
                       {"track_type": "audio", "track_index": 1}).get("items")
    state.phase("append_audio")
    items_after = len(items) if isinstance(items, list) else 0
    state.report["audio_placement"] = {
        "source_frame_count": state.source_frames,
        "audio_track_count_before": count_before if isinstance(count_before, int) else 0,
        "audio_track_items_after": items_after,
    }
    if not ok_payload(appended) or items_after < 1:
        state.fail("error", "append_audio")


def _choose_language(state: FlowState) -> None:
    candidates: list[dict[str, object]] = []
    for value in logic.LANGUAGE_CANDIDATES:
        echo = state.call("timeline", "subtitle_generation_probe",
                          {"settings": {"language": value}})
        verdict = logic.classify_candidate(echo)
        ignored = echo.get("ignored_settings")
        settings = echo.get("settings")
        candidates.append({
            "value": value, "accepted": verdict == "accepted",
            "ignored_keys": ([str(k) for k in ignored]
                             if isinstance(ignored, list) else []),
            "echo_setting_count": len(settings) if isinstance(settings, dict) else 0,
        })
        if verdict == "accepted":
            state.chosen = value
            break
    state.phase("settings_echo")
    state.report["settings_candidates"] = candidates
    if state.chosen is None:
        state.fail("settings-rejected", "settings_echo")


def _generate(state: FlowState) -> None:
    if state.chosen is None:
        raise logic.ProbeRefusalError("error", "chosen-missing")
    generation: dict[str, object] = {}
    try:
        gen = state.call("timeline", "subtitle_generation_probe",
                         {"allow_generate": True,
                          "settings": {"language": state.chosen}},
                         timeout_seconds=GENERATE_TIMEOUT_S)
    except BaseException as exc:  # noqa: BLE001 (classified; finally cleanup)
        verdict = classify_exception(exc)
        if state.failure is None:
            state.failure = verdict
        generation = {"exception": verdict}
    else:
        generation = _generation_echo(gen)
    state.phase("generate")
    state.report["generation"] = generation


def _generation_echo(gen: dict[str, object]) -> dict[str, object]:
    ignored = gen.get("ignored_settings")
    settings = gen.get("settings")
    return {
        "success": gen.get("success") is True, "verified": gen.get("verified") is True,
        "subtitle_tracks_before": gen.get("subtitle_tracks_before"),
        "subtitle_tracks_after": gen.get("subtitle_tracks_after"),
        "ignored_keys": ([str(k) for k in ignored]
                         if isinstance(ignored, list) else []),
        "echo_setting_count": len(settings) if isinstance(settings, dict) else 0,
    }


def _read_back(state: FlowState) -> None:
    rows = state.call("timeline", "get_items_in_track",
                      {"track_type": "subtitle", "track_index": 1})
    transcript = state.call("timeline", "get_transcript", {"with_timecodes": True})
    state.phase("readback")
    items = rows.get("items")
    raw_items = [dict(r) for r in items] if isinstance(items, list) else []
    transcript_cues = transcript.get("cues")
    transcript_count = (len(transcript_cues)
                        if isinstance(transcript_cues, list) else 0)
    generation = state.report.get("generation")
    # Vendor success AND verified are both required; stale rows never
    # rescue a failed generation (plan Stop rules: success=false => unproven).
    succeeded = (isinstance(generation, dict)
                 and generation.get("success") is True
                 and generation.get("verified") is True)
    if not ok_payload(rows):
        state.fail("error", "readback")
    elif not succeeded:
        state.fail("generation-failed", "readback")
    elif not raw_items:
        state.fail("readback-empty", "readback")
    else:
        _validate_cues(state, raw_items, transcript_count)


def _validate_cues(state: FlowState, raw_items: list[dict[str, object]],
                   transcript_count: int) -> None:
    try:
        cues, canonical_sha = logic.canonical_cues(
            [{"text": row.get("name"), "start": row.get("start"),
              "end": row.get("end")} for row in raw_items])
    except logic.ProbeRefusalError as exc:
        state.fail(exc.reason if exc.reason != "error" else "readback-invalid",
                   "readback")
        cues, canonical_sha = [], ""
    start = state.timeline_start
    lo = min((c[1] for c in cues), default=0)
    hi = max((c[2] for c in cues), default=0)
    state.report["readback"] = {
        "item_count": len(cues), "transcript_cue_count": transcript_count,
        "min_start_frame": lo, "max_end_frame": hi,
        "relative_min_start_frame": lo - start, "relative_max_end_frame": hi - start,
        "all_text_nonempty": True, "canonical_sha256": canonical_sha,
    }
    state.sink.write("readback.json", {
        "items": raw_items, "cues": logic.cues_json(cues),
        "transcript_cue_count": transcript_count,
        "timeline_start_frame": start,
    })


_PHASES = (_prepare_project, _ensure_timeline, _import_media, _place_audio,
           _choose_language, _generate, _read_back)


def run_flow(call: ActionSeam, sink: Sink, media: MediaInput,
             run_label: str) -> dict[str, object]:
    """One labeled disposable session (``r1``/``r2``) up to disposal."""
    if run_label not in RUN_LABELS:
        raise logic.ProbeRefusalError("error", "run label not in closed set")
    state = FlowState(call=call, sink=sink, media=media, run_label=run_label)
    state.report = {"schema_version": "resolve-auto-caption-capability-v1",
                    "run": run_label,
                    "project_name": f"probe-resolve-autocap-{run_label}",
                    "timeline_name": TIMELINE_NAME,
                    "input_media_sha256": media.expected_sha256}
    if media.actual_sha256 != media.expected_sha256:
        state.report.update(verdict="media-mismatch",
                            failure={"reason": "media-mismatch",
                                     "phase": "media_verify"})
        return state.report
    loaded = call("project_manager", "load", {"name": state.report["project_name"]})
    state.phase("project_exists_check")
    if ok_payload(loaded):
        state.report.update(verdict="project-exists",
                            failure={"reason": "project-exists",
                                     "phase": "project_exists_check"})
        return state.report
    for step in _PHASES:
        if state.failure is None:
            step(state)
    state.report["readback"] = state.report.get("readback", {"item_count": 0})
    state.report["verdict"] = state.failure or "generated-and-read"
    if state.failure is not None:
        state.report["failure"] = {"reason": state.failure, "phase": "flow"}
    state.report["elapsed_ms"] = round((time.monotonic() - state.t0) * 1000, 1)
    return state.report


__all__ = ["run_flow"]
