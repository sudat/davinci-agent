# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 5 live probe: the PRODUCT adapter drives the pinned MCP end to end.

Flow (one session, one writer): disposable project prepare → calibrated
synthetic import → placement → voice isolation set/get → dialogue-chain
preset stage (expected: typed ``preset-missing`` until the operator saves
the named Fairlight mix) → direct typed apply probe of the bound name →
loudness/peak QC stage with a REAL Resolve render measured by the pinned
QC stack. Renders and scratch media are deleted at exit; only the summary
and the fresh call ledger are retained (byte size reported).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import t5_session
from t5_session import (
    LEDGER_DIR,
    RENDER_DIR,
    SCRATCH,
    TIMELINE_NAME,
    close_session,
    generate_calibrated_clip,
    open_session,
    reset_evidence_tree,
    scrub_repo_prefixes,
)

VIDEO_PIPELINE = t5_session.VIDEO_PIPELINE
sys.path.insert(0, str(VIDEO_PIPELINE))

from services.mcp_client.ops import McpOps  # noqa: E402 (post-bootstrap)
from services.mcp_execution.audio_measurement import default_audio_measurement  # noqa: E402
from services.mcp_execution.live_adapter import (  # noqa: E402
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveMcpAdapter,
)

PRESET_NAME = "dialogue-chain"
PRESET_REF = "fairlight-dialogue-chain-v1"
LOUDNESS_RANGE = (-17.0, -13.0)


def _adapter(state: dict[str, object], clip: Path) -> LiveMcpAdapter:
    client = state["client"]  # type: ignore[index]
    state["ops"] = McpOps(client)  # type: ignore[index]

    def transport(tool: str, action: str, params: dict[str, object]) -> object:
        return client._call_action_json(tool, action, params)  # noqa: SLF001 (the probe drives the product adapter through the client's action seam)

    return LiveMcpAdapter(
        transport,  # type: ignore[arg-type]
        media_paths={"src-t5": str(clip)},
        audio_measure=default_audio_measurement(),
        render_dir=str(RENDER_DIR),
    )


def _stage_params(
    stage: str, metric: str, minimum: float, maximum: float, unit: str
) -> dict[str, object]:
    return {
        "action": "apply_audio_stage",
        "stage": stage,
        "goal": f"{stage} for the dialogue chain",
        "metric": metric,
        "minimum": minimum,
        "maximum": maximum,
        "unit": unit,
        "preset_ref": PRESET_REF,
    }


def _row(name: str, status: str, detail: str) -> dict[str, object]:
    return {"step": name, "status": status, "detail": detail}


def _run_loudness(adapter: LiveMcpAdapter) -> dict[str, object]:
    """One loudness-QC stage run; a typed refusal lands in the row detail."""
    try:
        result = adapter(
            "render_boundary_report",
            "apply_audio_stage",
            _stage_params(
                "loudness_peak_qc",
                "integrated_loudness",
                LOUDNESS_RANGE[0],
                LOUDNESS_RANGE[1],
                "lufs",
            ),
        )
    except LiveAdapterError as error:
        return {
            "result": None,
            "row": _row("loudness_peak_qc", "blocked", f"{error.code}: {error.detail}"),
        }
    measurements = result.get("measurements") if isinstance(result, dict) else None
    return {
        "result": measurements,
        "row": _row(
            "loudness_peak_qc",
            "ok",
            f"value={result.get('value') if isinstance(result, dict) else None}",
        ),
    }


def _write_summary(summary: dict[str, object]) -> None:
    path = Path(__file__).resolve().parent / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def run_probe() -> int:  # noqa: PLR0915 (frozen linear probe main)
    reset_evidence_tree()
    started = time.monotonic()
    state = open_session()
    rows: list[dict[str, object]] = []
    blocker: dict[str, object] | None = None
    measurements: dict[str, object] | None = None
    try:
        clip = generate_calibrated_clip()
        adapter = _adapter(state, clip)
        adapter(
            "prepare_project",
            "prepare_project",
            {"action": "prepare_project", "timeline_name": TIMELINE_NAME, "fps_num": 30, "fps_den": 1},  # noqa: E501
        )
        rows.append(_row("prepare_project", "ok", f"project/timeline {TIMELINE_NAME}"))
        adapter("safe_import_media", "import_media", {"action": "import_media", "source_id": "src-t5"})  # noqa: E501
        rows.append(_row("safe_import_media", "ok", "calibrated synthetic clip imported"))
        adapter(
            "append_to_timeline",
            "place_clip",
            {
                "action": "place_clip",
                "item_id": "t5-1",
                "source": {
                    "source_id": "src-t5",
                    "span": {"start_frame": 0, "end_frame": 180, "rate": {"num": 30, "den": 1}},
                },
                "record_span": {"start_frame": 0, "end_frame": 180},
                "track_role": "primary",
                "av_link_id": None,
            },
        )
        rows.append(_row("append_to_timeline", "ok", "180-frame clip placed on the primary track"))
        adapter(
            "append_to_timeline",
            "place_audio",
            {
                "action": "place_audio",
                "item_id": "t5-a1",
                "source": {
                    "source_id": "src-t5",
                    "span": {"start_frame": 0, "end_frame": 180, "rate": {"num": 30, "den": 1}},
                },
                "record_span": {"start_frame": 0, "end_frame": 180},
                "audio_role": "dialogue",
                "av_link_id": None,
            },
        )
        rows.append(
            _row("append_to_timeline(place_audio)", "ok", "dialogue audio placed (media_type 2)")
        )
        # Loudness FIRST (voice isolation off): the calibrated tone must
        # measure INSIDE the plan range — the pass-side native proof.
        loudness_pass = _run_loudness(adapter)
        if loudness_pass["result"] is None:
            loudness_pass["row"]["status"] = "failed"
        rows.append(loudness_pass["row"])
        if loudness_pass["result"] is not None:
            measurements = loudness_pass["result"]
        voice = adapter(
            "set_voice_isolation_state",
            "apply_voice_isolation",
            {
                "action": "apply_voice_isolation",
                "effect_kind": "voice_isolation",
                "stage": None,
                "target_item_id": None,
                "note": "",
            },
        )
        rows.append(
            _row("voice_isolation", "ok", json.dumps(voice, ensure_ascii=False, sort_keys=True))
        )

        presets = state["ops"].fairlight_presets()
        rows.append(
            _row(
                "get_fairlight_presets",
                "ok" if presets.ok else "error",
                f"listing={list(presets.presets)}",
            )
        )
        apply_probe = state["ops"].apply_fairlight_preset(PRESET_NAME)
        rows.append(
            _row(
                "apply_fairlight_preset",
                "ok" if apply_probe.ok else "refused",
                f"success={apply_probe.success} error={apply_probe.error}",
            )
        )

        try:
            adapter(
                "safe_set_audio_properties",
                "apply_audio_stage",
                _stage_params("dialogue_cleanup", "noise_reduction", 3.0, 12.0, "db"),
            )
        except LiveAdapterUnsupportedError as error:
            blocker = {
                "code": error.code,
                "detail": error.detail,
                "operator_instruction": (
                    f"Fairlight ページでミックスを {PRESET_NAME!r} という名前で一度保存する"
                    "(プリセット保存 API は存在しない - UI 操作のみ)。保存後に再実行。"
                ),
            }
            rows.append(_row("dialogue_cleanup", "blocked", f"{error.code}: {error.detail}"))
        else:
            rows.append(
                _row("dialogue_cleanup", "unexpected-success", "preset absent - success impossible")
            )

        # With voice isolation ON, a non-voice tone measures far below the
        # range — the live proof that out-of-range output BLOCKS completion.
        loudness_blocked = _run_loudness(adapter)
        if loudness_blocked["result"] is not None:
            rows.append(
                _row("loudness_peak_qc_after_voice_isolation", "unexpected-success", "must block")
            )
        else:
            rows.append(
                _row(
                    "loudness_peak_qc_after_voice_isolation",
                    "blocked",
                    str(loudness_blocked["row"]["detail"]),
                )
            )
    except Exception as error:  # noqa: BLE001 (probe isolation: record, clean up, exit non-zero)
        rows.append(_row("probe", "failed", f"{type(error).__name__}: {error}"))
    finally:
        close_session(state)

    render_files = sorted(path.name for path in RENDER_DIR.glob("*")) if RENDER_DIR.is_dir() else []
    summary = {
        "schema_version": "task5-audio-probe-v1",
        "resolve_version": state.get("resolve_version"),
        "provider_version": state.get("provider_version"),
        "preset_binding": {
            "ref": PRESET_REF,
            "name": PRESET_NAME,
            "kit_recipe_id": "audio/dialogue-chain",
        },
        "steps": rows,
        "cleanup_blocker": blocker,
        "loudness_measurements": measurements,
        "renders_before_cleanup": render_files,
        "wall_seconds": round(time.monotonic() - started, 1),
    }
    for path in (SCRATCH, RENDER_DIR):
        if path.exists():
            for entry in sorted(path.glob("*")):
                entry.unlink()
    summary_path = Path(__file__).resolve().parent / "summary.json"
    _write_summary(summary)
    scrub_repo_prefixes()
    retained = sorted(path.name for path in LEDGER_DIR.glob("*")) if LEDGER_DIR.is_dir() else []
    retained_bytes = sum(p.stat().st_size for p in LEDGER_DIR.glob("*"))
    retained_bytes += summary_path.stat().st_size
    summary["retained_ledger_files"] = retained
    summary["retained_bytes"] = retained_bytes
    _write_summary(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = any(row["status"] in {"failed", "unexpected-success"} for row in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_probe())
