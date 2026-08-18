"""Offline fake evidence synthesis for the Phase-1 gate fault scenarios.

Builds a COMPLETE five-fixture evidence tree purely from the frozen goldens
(no chain run, no ffmpeg): fake run reports, review bundles, review-store
plan/IR files whose bytes golden-match, review-step observations, and the
approval probes. The baseline synthesis satisfies every check (the fault CLI
asserts this); each fault knob then injects exactly one defect.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from services.foundation_io import atomic_write, sha256_file
from services.job_runner.gate_p1_models import (
    BUNDLE_FILE,
    FixtureObservation,
    ReviewStepRecord,
)

FAULTS = (
    "incomplete_flow",
    "must_include_miss",
    "coordinate_defect",
    "coverage_below_80",
    "auto_created_operator_record",
)


@dataclass(frozen=True, slots=True)
class FaultKnobs:
    fault: str


class FakeEvidenceError(Exception):
    pass


def _obj_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise FakeEvidenceError("golden table is not an array")
    return value


def _as_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FakeEvidenceError(f"{label} is not an integer: {value!r}")
    return value


def _obj(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FakeEvidenceError(f"{label} is not an object")
    return value


def _rate(manifest: Mapping[str, object]) -> dict[str, int]:
    source = _obj(manifest["edit_source"], "edit_source")
    return {
        "num": int(source["frame_rate_num"]),  # type: ignore[arg-type]
        "den": int(source["frame_rate_den"]),  # type: ignore[arg-type]
    }


def _ir_document(rows: list[Mapping[str, object]], manifest: Mapping[str, object]) -> object:
    """Golden plan rows -> review-plane-shaped IR JSON (disclosed id mapping)."""

    tracks: dict[int, list[dict[str, object]]] = {}
    rate = _rate(manifest)
    video_pos = 0
    for row in rows:
        span = _obj(row["span"], "span")
        source = _obj(row["source_span"], "source_span")
        track = int(row["track_index"])  # type: ignore[arg-type]
        kind = str(row["kind"])
        if kind == "video":
            video_pos += 1
            item_id = str(row["segment_id"])
        elif kind == "audio":
            item_id = f"a{video_pos}"
        else:
            item_id = str(row["item_id"])
        tracks.setdefault(track, []).append(
            {
                "item_id": item_id,
                "kind": row["kind"],
                "source": {
                    "span": {
                        "start_frame": source["start_frame"],
                        "end_frame": source["end_frame"],
                        "rate": rate,
                    }
                },
                "record_span": {
                    "start_frame": span["start_frame"],
                    "end_frame": span["end_frame"],
                },
                "av_link_id": row.get("av_link_id"),
                "subtitle_text": row.get("subtitle_text"),
            }
        )
    return {
        "tracks": [
            {
                "track": {
                    "kind": {1: "video", 2: "audio", 3: "subtitle"}.get(index, "subtitle"),
                    "index": index,
                },
                "items": items,
            }
            for index, items in sorted(tracks.items())
        ]
    }


def _write_run(
    work_root: Path,
    manifest: Mapping[str, object],
    golden: Mapping[str, object],
    fixture_id: str,
    knobs: FaultKnobs,
) -> FixtureObservation:
    run1 = work_root / "run1"
    run2 = work_root / "run2"
    store1 = run1 / "review-store"
    store2 = run2 / "review-store"
    store1.mkdir(parents=True, exist_ok=True)
    store2.mkdir(parents=True, exist_ok=True)
    plan_rows = _obj_list(golden["plan_items"])
    initial_rows: list[Mapping[str, object]] = [
        row for row in plan_rows if isinstance(row, dict)
    ]
    if knobs.fault == "must_include_miss" and fixture_id == "p1-ref-03-multi-take-must-include":
        initial_rows = [row for row in initial_rows if row.get("segment_id") != "t2a"]
    if knobs.fault == "coordinate_defect" and fixture_id == "p1-ref-01-clean-ja":
        victim = dict(initial_rows[0])
        span = dict(victim["span"])  # type: ignore[index]
        span["end_frame"] = int(span["end_frame"]) - 1  # type: ignore[arg-type]
        victim["span"] = span
        initial_rows[0] = victim
    ir1 = _ir_document(initial_rows, manifest)
    atomic_write(store1 / "ir-v1.json", json.dumps(ir1, sort_keys=True).encode())
    atomic_write(store2 / "ir-v1.json", json.dumps(ir1, sort_keys=True).encode())
    atomic_write(store1 / "plan-v1.json", b"fake-initial-plan")
    atomic_write(store2 / "plan-v1.json", b"fake-initial-plan")
    final_table = _obj_list(golden["review_final_plan_items"])
    final_rows: list[Mapping[str, object]] = [
        row for row in final_table if isinstance(row, dict)
    ]
    outcomes = _obj_list(golden["review_outcomes"])
    applies = sum(
        1 for row in outcomes if isinstance(row, dict) and row.get("decision") == "apply"
    )
    final_version = f"v{1 + applies}"
    if final_version != "v1":
        ir_final = _ir_document(final_rows, manifest)
        for store in (store1, store2):
            atomic_write(
                store / f"ir-{final_version}.json",
                json.dumps(ir_final, sort_keys=True).encode(),
            )
            atomic_write(store / f"plan-{final_version}.json", b"fake-final-plan")
    common = {
        "selection_plan_sha256": hashlib.sha256(b"fake-selection").hexdigest(),
        "edit_plan_sha256": hashlib.sha256(b"fake-edit").hexdigest(),
        "production_ir_sha256": hashlib.sha256(b"fake-production").hexdigest(),
        "review_plan_sha256": hashlib.sha256(b"fake-review-plan").hexdigest(),
        "review_ir_sha256": sha256_file(store1 / "ir-v1.json"),
        "stop_stage": "PREVIEW_READY",
    }
    report = {"episode_id": fixture_id, **common}
    atomic_write(run1 / "run-report.json", json.dumps(report, sort_keys=True).encode())
    atomic_write(run2 / "run-report.json", json.dumps(report, sort_keys=True).encode())
    initial_sha = sha256_file(store1 / "ir-v1.json")
    final_sha = (
        sha256_file(store1 / f"ir-{final_version}.json")
        if final_version != "v1"
        else initial_sha
    )
    bundle = {
        "initial": {"plan_version": "v1", "plan_sha256": "0" * 64, "ir_sha256": initial_sha,
                    "preview_sha256": hashlib.sha256(b"fake-preview-1").hexdigest()},
        "current": {"plan_version": final_version, "plan_sha256": "0" * 64,
                    "ir_sha256": final_sha,
                    "preview_sha256": hashlib.sha256(b"fake-preview-f").hexdigest()},
    }
    for run in (run1, run2):
        atomic_write(run / BUNDLE_FILE, json.dumps(bundle, sort_keys=True).encode())
    steps: list[ReviewStepRecord] = []
    outcomes = _obj_list(golden["review_outcomes"])
    for index, outcome in enumerate(outcomes, start=1):
        row = _obj(outcome, "review_outcome")
        structured = row.get("classification") in ("clear", "ambiguous")
        if knobs.fault == "coverage_below_80" and index == 1:
            structured = False
        steps.append(
            ReviewStepRecord(
                command_index=_as_int(row.get("command_index", index), "command_index"),
                operation=str(row.get("operation", "correct_subtitle")),
                status="proposal" if structured else "error",
                classification=str(row.get("classification", "clear")),
                decision="applied" if row.get("decision") == "apply" else "deferred",
                structured=structured,
                candidate_item_ids=tuple(
                    str(item)
                    for item in _obj_list(row.get("target_candidate_ids") or [])
                ),
            )
        )
    operations = [
        {"name": "chain-run", "result": "ok", "detail": f"{fixture_id}:PREVIEW_READY"},
        {"name": "chain-run", "result": "ok", "detail": f"{fixture_id}:PREVIEW_READY"},
        {"name": "bundle-rehash", "result": "ok", "detail": ""},
        {"name": "schema-gap-refused", "result": "refused", "detail": "replay_mismatch"},
    ]
    if knobs.fault == "incomplete_flow" and fixture_id == "p1-ref-02-pauses-fillers":
        operations[1] = {
            "name": "chain-run",
            "result": "chain-error",
            "detail": "stop:ANALYZED",
        }
    from services.job_runner.gate_cp_models import OperationOutcome  # noqa: PLC0415

    return FixtureObservation(
        fixture_id=fixture_id,
        work_dir=str(work_root),
        run1_dir=str(run1),
        run2_dir=str(run2),
        operations=tuple(OperationOutcome.model_validate(op) for op in operations),
        review_steps=tuple(steps),
        schema_gap_refused=True,
        fields={
            "run1_report": str(run1 / "run-report.json"),
            "run2_report": str(run2 / "run-report.json"),
            "store_dir": str(store1),
        },
    )


__all__ = ["FAULTS", "FaultKnobs"]
