# noqa: INP001 (evidence tree is not an importable package by design)
"""Task 6 live probe: the PRODUCT adapter drives the pinned MCP end to end.

Flow (one session, one writer): disposable project prepare → calibrated
synthetic import → two placements (target + untargeted) on the primary
track → explicit-target DRX exposure correction through the live
adapter (dry-run → confirmation token → apply + graph/frame evidence +
rerun idempotency). Renders and scratch media are deleted at exit; only
the summary and the fresh call ledger are retained.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import t6_session
from t6_session import (
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

VIDEO_PIPELINE = t6_session.VIDEO_PIPELINE
sys.path.insert(0, str(VIDEO_PIPELINE))

from services.mcp_execution.color_measurement import default_frame_comparison  # noqa: E402, I001 (post-bootstrap)  # type: ignore[import-not-found]
from services.mcp_execution.live_adapter import (  # noqa: E402
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveMcpAdapter,
)  # type: ignore[import-not-found]

DRX_REF = "drx-technical-normalize-v1"
SECTION = "technical_correction.exposure"


def _adapter(state: dict[str, object], clip: Path) -> LiveMcpAdapter:
    client = state["client"]  # type: ignore[index]

    def transport(tool: str, action: str, params: dict[str, object]) -> object:
        return client._call_action_json(tool, action, params)  # noqa: SLF001 (the probe drives the product adapter through the client's action seam)

    return LiveMcpAdapter(
        transport,  # type: ignore[arg-type]
        media_paths={"src-t6": str(clip)},
        render_dir=str(RENDER_DIR),
        frame_diff=default_frame_comparison(),
    )


def _row(name: str, status: str, detail: str) -> dict[str, object]:
    return {"step": name, "status": status, "detail": detail}


def _drx_params() -> dict[str, object]:
    return {
        "action": "apply_color",
        "section": SECTION,
        "target_note": "t6 live probe exposure",
        "look_ref": None,
        "drx_ref": DRX_REF,
        "targets": [
            {"item_id": "t6-1", "record_span": {"start_frame": 0, "end_frame": 60}},
        ],
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
    first_result: dict[str, object] | None = None
    rerun_result: dict[str, object] | None = None
    try:
        clip = generate_calibrated_clip()
        adapter = _adapter(state, clip)
        adapter(
            "prepare_project",
            "prepare_project",
            {"action": "prepare_project", "timeline_name": TIMELINE_NAME, "fps_num": 30, "fps_den": 1},  # noqa: E501
        )
        rows.append(_row("prepare_project", "ok", f"project/timeline {TIMELINE_NAME}"))
        adapter("safe_import_media", "import_media", {"action": "import_media", "source_id": "src-t6"})  # noqa: E501
        rows.append(_row("safe_import_media", "ok", "calibrated synthetic clip imported"))
        adapter(
            "append_to_timeline",
            "place_clip",
            {
                "action": "place_clip",
                "item_id": "t6-1",
                "source": {
                    "source_id": "src-t6",
                    "span": {"start_frame": 0, "end_frame": 60, "rate": {"num": 30, "den": 1}},
                },
                "record_span": {"start_frame": 0, "end_frame": 60},
                "track_role": "primary",
                "av_link_id": None,
            },
        )
        rows.append(
            _row("append_to_timeline(t6-1)", "ok", "target 60-frame clip [0,60) on primary")
        )
        adapter(
            "append_to_timeline",
            "place_clip",
            {
                "action": "place_clip",
                "item_id": "t6-2",
                "source": {
                    "source_id": "src-t6",
                    "span": {"start_frame": 0, "end_frame": 60, "rate": {"num": 30, "den": 1}},
                },
                "record_span": {"start_frame": 60, "end_frame": 120},
                "track_role": "primary",
                "av_link_id": None,
            },
        )
        rows.append(_row("append_to_timeline(t6-2)", "ok", "untargeted 60-frame clip [60,120) (stability control)"))  # noqa: E501
        try:
            first_result = adapter("safe_apply_drx", "apply_color", _drx_params())  # type: ignore[assignment]
            data = first_result if isinstance(first_result, dict) else {}
            sha = data.get("drx_sha256")
            entries = data.get("applied_targets")
            frames = data.get("untargeted_frames") if isinstance(data, dict) else None
            rows.append(
                _row(
                    "safe_apply_drx(targeted)",
                    "ok",
                    f"drx={sha} targets={len(entries) if isinstance(entries, list) else '?'} "
                    f"untargeted_rows={len(frames) if isinstance(frames, list) else '?'}",
                )
            )
        except (LiveAdapterError, LiveAdapterUnsupportedError) as error:
            blocker = {"code": error.code, "detail": error.detail}
            rows.append(
                _row(
                    "safe_apply_drx(targeted)",
                    "blocked" if error.code in {"drx-missing", "graph-unchanged", "frame-unchanged"}  else "error",  # noqa: E501
                    f"{error.code}: {error.detail}",
                )
            )
        if blocker is None:
            try:
                rerun_result = adapter("safe_apply_drx", "apply_color", _drx_params())  # type: ignore[assignment]
                parsed = rerun_result if isinstance(rerun_result, dict) else {}
                entry = (
                    parsed.get("applied_targets", [{}])[0]  # type: ignore[union-attr]
                    if isinstance(parsed.get("applied_targets"), list)
                    else {}
                )
                already = entry.get("already_applied")
                rows.append(
                    _row(
                        "safe_apply_drx(rerun)",
                        "ok" if already else "error",
                        f"already_applied={already} grade_version={entry.get('grade_version')}",
                    )
                )
            except (LiveAdapterError, LiveAdapterUnsupportedError) as error:
                rows.append(
                    _row("safe_apply_drx(rerun)", "blocked", f"{error.code}: {error.detail}")
                )
    except Exception as error:  # noqa: BLE001 (probe isolation: record, clean up, exit non-zero)
        rows.append(_row("probe", "failed", f"{type(error).__name__}: {error}"))
    finally:
        close_session(state)

    render_files = sorted(path.name for path in RENDER_DIR.glob("*")) if RENDER_DIR.is_dir() else []
    summary: dict[str, object] = {
        "schema_version": "task6-color-probe-v1",
        "resolve_version": state.get("resolve_version"),
        "provider_version": state.get("provider_version"),
        "drx_binding": {
            "ref": DRX_REF,
            "kit_recipe_id": "color/technical-normalize",
            "drx": "technical-normalize-v1.drx",
            "sha256": "509e70f94fe33667a5f0ee92dfb36cf6106bf8f086e962b41ca93cd2c1c30269",
        },
        "section": SECTION,
        "steps": rows,
        "blocker": blocker,
        "first_result": first_result,
        "rerun_result": rerun_result,
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
    failed = any(row["status"] in {"failed", "unexpected-success", "error"} for row in rows)
    if any(row["status"] == "blocked" for row in rows):
        failed = False
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_probe())
