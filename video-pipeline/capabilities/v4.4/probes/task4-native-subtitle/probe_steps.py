# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 4 probe sequence: session, product-adapter placement, cue verdicts.

Each probe returns ``(status, readback)`` and is executed through
``live_support.run_probe`` so the ledger captures every vendor call. The
sequence is ordered: disposable session → product adapter prepare + media
placement → auto-caption expressibility → one cue → rerun identity → all
cues → (render proof lives in ``render_evidence``) → independent readback.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from evidence_io import EVIDENCE
from mcp_call import must, raw
from probe_session import BOUND_FONT, CUE1, CUES, ProbeState, params_for

from services.mcp_execution.live_adapter import LiveMcpAdapter

if TYPE_CHECKING:
    from live_support import LiveSession

    from services.mcp_client.ops import McpOps


def build_media_track(session: LiveSession, state: ProbeState, tl_name: str) -> None:
    must(raw(session, "timeline", "set_current", {"name": tl_name}), "set_current")
    must(
        raw(
            session,
            "media_pool",
            "append_to_timeline",
            {
                "clip_infos": [
                    {
                        "clip_id": state["clip_id"],
                        "media_type": 1,
                        "start_frame": 0,
                        "end_frame": 150,
                        "record_frame": state["timeline_start"],
                        "record_frame_mode": "absolute",
                        "track_index": 1,
                    }
                ]
            },
        ),
        "append media V1",
    )


def probe_p1_session(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    name = f"probe-t4r-{time.strftime('%H%M%S')}"
    state["project_name"] = name
    prepared = session.ops.prepare_project(name, 30)
    if not prepared.ok:
        return "failed", f"prepare_project: {prepared.error}"
    ensured = session.ops.ensure_timeline(f"{name}-tl")
    if not ensured.ok:
        return "failed", f"ensure_timeline: {ensured.error}"
    current = session.ops.get_current_timeline()
    if not current.ok or current.start_frame is None:
        return "failed", "no current timeline start_frame"
    state["timeline_start"] = int(current.start_frame)
    state["timeline_name"] = str(current.name)
    imported = session.ops.safe_import_media([str(session.media["parity-src-002"])])
    if not imported.ok or not imported.clips:
        return "failed", f"safe_import_media: {imported.error}"
    state["clip_id"] = imported.clips[0].clip_id
    return "ok", f"ts={state['timeline_start']} clip={state['clip_id']}"


def probe_a_autocaption(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    echo = session.ops.subtitle_generation_probe()
    raw(
        session,
        "timeline",
        "subtitle_generation_probe",
        {"allow_generate": True, "settings": {"language": "auto"}},
    )
    time.sleep(2.0)
    items = raw(
        session, "timeline", "get_items_in_track",
        {"track_type": "subtitle", "track_index": 1},
    )
    raw_items = items.get("items", [])
    cues = [dict(row) for row in raw_items] if isinstance(raw_items, list) else []
    state["a_echo"] = echo.model_dump(mode="json")
    settings_keys = sorted((echo.settings or {}).keys())
    return "ok", (
        f"settings keys={settings_keys}; no cue text/timing parameter exists; "
        f"generated cues (speech-to-text, tone audio)={cues}; "
        "committed text cannot be expressed through this path"
    )


def probe_p2_prepare_product_adapter(
    session: LiveSession, state: ProbeState
) -> tuple[str, str]:
    """The product seam, driven exactly as the runner would: ONE adapter
    prepare (single project load), then the media placement with a
    post-load clip id. Repeated loads invalidate import-time media pool
    ids (measured), so placements always follow the one load."""
    client = session.client

    def transport(tool: str, action: str, params: Mapping[str, object]) -> object:
        return client._call_action_json(tool, action, params)  # noqa: SLF001 (product adapter seam: the probe hands the adapter the same raw transport the runner does)

    adapter = LiveMcpAdapter(transport, media_paths={})
    adapter(
        "prepare_project",
        "prepare_project",
        {
            "action": "prepare_project",
            "timeline_name": str(state["timeline_name"]),
            "fps_num": 30,
            "fps_den": 1,
        },
    )
    reimported = session.ops.safe_import_media([str(session.media["parity-src-002"])])
    if not reimported.ok or not reimported.clips:
        return "failed", f"post-load reimport: {reimported.error}"
    state["clip_id"] = reimported.clips[0].clip_id
    state["adapter"] = adapter
    build_media_track(session, state, str(state["timeline_name"]))
    return "ok", "adapter prepared; media V1 [0,150) placed with post-load id"


def probe_b_one_cue(_session: LiveSession, state: ProbeState) -> tuple[str, str]:
    adapter = state["adapter"]
    result = adapter("subtitle_generation_probe", "apply_subtitles", params_for((CUE1,)))
    state["b_one_cue"] = result
    cue = result["cues"][0]
    ok = (
        cue["text"] == CUE1["text"]
        and cue["record_span"] == {"start_frame": 15, "end_frame": 45}
        and cue["style"]["font"] == BOUND_FONT
    )
    return ("ok" if ok else "failed"), json.dumps(result, ensure_ascii=False)[:400]


def probe_b2_rerun(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    adapter = state["adapter"]
    items_before = raw(
        session, "timeline", "get_items_in_track",
        {"track_type": "video", "track_index": 2},
    ).get("items", [])
    result = adapter("subtitle_generation_probe", "apply_subtitles", params_for((CUE1,)))
    items_after = raw(
        session, "timeline", "get_items_in_track",
        {"track_type": "video", "track_index": 2},
    ).get("items", [])
    state["b2_rerun"] = {
        "before_count": len(items_before),
        "after_count": len(items_after),
        "result": result,
    }
    if len(items_before) != len(items_after) or len(items_after) != 1:
        return "failed", f"duplicate created: {len(items_before)} -> {len(items_after)}"
    return "ok", f"rerun duplicate_created=0 overlay_items={len(items_after)}"


def probe_c_all_cues(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    adapter = state["adapter"]
    result = adapter("subtitle_generation_probe", "apply_subtitles", params_for(CUES))
    state["c_all_cues"] = result
    ts = state["timeline_start"]
    spans = sorted(
        (int(i["start"]) - ts, int(i["end"]) - ts)
        for i in raw(
            session, "timeline", "get_items_in_track",
            {"track_type": "video", "track_index": 2},
        ).get("items", [])
    )
    expected = sorted((int(c["start_frame"]), int(c["end_frame"])) for c in CUES)
    texts_ok = [row["text"] for row in result["cues"]] == [str(c["text"]) for c in CUES]
    styles_ok = all(row["style"]["font"] == BOUND_FONT for row in result["cues"])
    newline_kept = "\n" in result["cues"][2]["text"]
    if spans != expected or not texts_ok or not styles_ok:
        detail = (
            f"spans={spans} expected={expected} "
            f"texts_ok={texts_ok} styles_ok={styles_ok}"
        )
        return "failed", detail
    if not newline_kept:
        return "failed", "long cue lost its line structure in readback"
    return "ok", f"3 cues exact: spans={spans} newline_preserved=True style_font={BOUND_FONT}"


def probe_prod_independent(session: LiveSession, state: ProbeState) -> tuple[str, str]:
    ops: McpOps = session.ops
    ts = state["timeline_start"]
    must(
        raw(session, "timeline", "set_current", {"name": state["timeline_name"]}),
        "set_current main",
    )
    items = ops.get_items_in_track("video", 2)
    spans = sorted((row.start - ts, row.end - ts) for row in items.items)
    texts, fonts = [], []
    for cue in CUES:
        ops.set_current_timeline(f"{state['timeline_name']}-cue-{cue['cue_id']}")
        text = ops.get_text_plus("Template")
        font = ops.get_fusion_input("Template", "Font")
        texts.append(text.text)
        fonts.append(font.value)
    ops.set_current_timeline(state["timeline_name"])
    expected = sorted((int(c["start_frame"]), int(c["end_frame"])) for c in CUES)
    report = {
        "schema_version": "task4-product-run-v2",
        "prepare": {"timeline_start": ts, "timeline_name": state["timeline_name"]},
        "independent_readback": {
            "overlay_item_count": len(items.items),
            "overlay_spans": spans,
            "expected_spans": expected,
            "card_texts": texts,
            "card_fonts": fonts,
            "bound_font": BOUND_FONT,
        },
    }
    ok = (
        len(items.items) == len(CUES)
        and spans == expected
        and texts == [str(c["text"]) for c in CUES]
        and fonts == [BOUND_FONT] * len(CUES)
    )
    report["verdict"] = "PASS" if ok else "FAIL"
    (EVIDENCE / "product-run.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False) + "\n"
    )
    readback = json.dumps(report["independent_readback"], ensure_ascii=False)[:400]
    return ("ok" if ok else "failed"), readback


__all__ = [
    "build_media_track",
    "probe_a_autocaption",
    "probe_b2_rerun",
    "probe_b_one_cue",
    "probe_c_all_cues",
    "probe_p1_session",
    "probe_p2_prepare_product_adapter",
    "probe_prod_independent",
]
