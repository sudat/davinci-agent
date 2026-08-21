from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import canonical_bytes, file_sha256

ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "ast", "common", "hashlib", "json", "pathlib", "sys"}
)
FIXTURE_IDS = (
    "p2-stale-capability",
    "p2-partial-build-restart",
    "p2-same-duration-wrong-media",
    "p2-false-render-complete",
    "p2-blocking-qc-privacy",
)
MANIFEST_DIR = Path("tests/fixtures/manifests/phase-2")
PHASE_1_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-1-technical")

# Frozen pinned Resolve facts (capability matrix + findings, Todo 19).
PINNED_MATRIX_PATH = "capabilities/resolve-21.0.4/capability-matrix.json"
PINNED_MATRIX_SHA256 = "0dee1905b9475fcf7655e6907bfc71db3e4b37ad620e40222010a49f25822599"
FRAME_ORIGIN = 108000
START_TIMECODE = "01:00:00:00"
SUBTITLE_MUX_ARGV = (
    "{ffmpeg}",
    "-nostdin",
    "-y",
    "-v",
    "error",
    "-i",
    "{render}",
    "-i",
    "{srt}",
    "-map",
    "0:v",
    "-map",
    "0:a",
    "-map",
    "1:0",
    "-c:v",
    "copy",
    "-c:a",
    "copy",
    "-c:s",
    "mov_text",
    "-metadata:s:s:0",
    "language=eng",
    "{output}",
)
CUE_STYLE_REF = "style-default-ja"
CUE_MIN_DURATION_FRAMES = 15
COMPLETION_FIELD = "CompletionPercentage"
COMPLETION_VALUE = 100


def load_manifest(fixture_id):
    import json

    return json.loads((MANIFEST_DIR / f"{fixture_id}.json").read_bytes())


def frame_to_ms(frames, num, den):
    total = frames * 1000 * den
    if total % num != 0:
        raise AssertionError(f"cue span {frames}f@{num}/{den} has non-integral milliseconds")
    return total // num


def render_complete(percentage):
    return percentage == COMPLETION_VALUE


def derive_package(base):
    av_rows = [row for row in base["base_records"] if row["kind"] in ("video", "audio")]
    placements = []
    for track_type in ("video", "audio"):
        for row in av_rows:
            if row["kind"] != track_type:
                continue
            placements.append(
                {
                    "item_id": row["item_id"],
                    "media_source_id": base["source_id"],
                    "capability": "base_cut",
                    "api_operation": "AppendToTimeline",
                    "clip_info": {
                        "media_source_id": base["source_id"],
                        "start_frame": row["source_start"],
                        "end_frame": row["source_end"],
                        "track_type": track_type,
                        "track_index": 1,
                        "record_frame": FRAME_ORIGIN + row["record_start"],
                    },
                }
            )
    link_order = []
    link_members = {}
    for row in av_rows:
        link_id = row["av_link_id"]
        if link_id is None:
            continue
        if link_id not in link_members:
            link_members[link_id] = []
            link_order.append(link_id)
        link_members[link_id].append(row["item_id"])
    link_groups = [
        {"av_link_id": link_id, "item_ids": sorted(link_members[link_id])}
        for link_id in link_order
    ]
    rate_num = base["frame_rate_num"]
    rate_den = base["frame_rate_den"]
    subtitle_rows = [row for row in base["base_records"] if row["kind"] == "subtitle"]
    subtitle_step = None
    if subtitle_rows:
        subtitle_step = {
            "rung": "external",
            "capability": "fixed_subtitle",
            "mux_operation": "ffmpeg-mov-text",
            "argv": list(SUBTITLE_MUX_ARGV),
            "cues": [
                {
                    "cue_id": row["item_id"],
                    "text": row["subtitle_text"],
                    "lines": [row["subtitle_text"]],
                    "style_ref": CUE_STYLE_REF,
                    "min_duration_frames": CUE_MIN_DURATION_FRAMES,
                    "anchor_record_start_frame": row["record_start"],
                    "anchor_record_end_frame": row["record_end"],
                    "start_ms": frame_to_ms(row["record_start"], rate_num, rate_den),
                    "end_ms": frame_to_ms(row["record_end"], rate_num, rate_den),
                }
                for row in subtitle_rows
            ],
        }
    render_job = {
        "capability": "render",
        "video_format": "MP4",
        "video_codec": "H264",
        "width": 1920,
        "height": 1080,
        "frame_rate": {"num": rate_num, "den": rate_den},
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "select_all_frames": True,
        "timeline_start_timecode": START_TIMECODE,
        "frame_origin": FRAME_ORIGIN,
        "extent_frames": max(row["record_end"] for row in av_rows),
        "completion": {
            "field": COMPLETION_FIELD,
            "value": COMPLETION_VALUE,
            "status_strings_parsed": False,
            "marks_bound_render_extent": False,
        },
    }
    return {
        "timeline": {
            "frame_rate": {"num": rate_num, "den": rate_den},
            "width": 1920,
            "height": 1080,
            "audio_sample_rate": base["audio_sample_rate"],
            "start_timecode": START_TIMECODE,
            "frame_origin": FRAME_ORIGIN,
        },
        "track_map": [
            {"logical_kind": "video", "logical_index": 1, "resolve_track_type": "video", "resolve_track_index": 1},
            {"logical_kind": "audio", "logical_index": 2, "resolve_track_type": "audio", "resolve_track_index": 1},
            {"logical_kind": "subtitle", "logical_index": 3, "placement": "post-render-external"},
        ],
        "placements": placements,
        "link_groups": link_groups,
        "subtitle_step": subtitle_step,
        "render_job": render_job,
        "inputs_view": {
            "capability_matrix": {"path": PINNED_MATRIX_PATH, "sha256": PINNED_MATRIX_SHA256},
            "declared_media": base["declared_media"],
        },
    }


def derive_route(fixture_id, manifest):
    fault = manifest["fault"]
    base = manifest["base"]
    kind = fault["kind"]
    route = {
        "package_compilation": fault["expected_package_compilation"],
        "failure_code": fault.get("expected_failure_code"),
        "readback": fault["expected_readback"],
        "render": fault["expected_render"],
        "qc": fault["expected_qc"],
        "retry": fault["expected_retry"],
        "human_route": fault["expected_human_route"],
    }
    derivation = ""
    if kind == "stale-capability":
        if fault["declared_matrix_sha256"] == PINNED_MATRIX_SHA256:
            raise AssertionError("stale-capability fault must differ from the pinned matrix")
        derivation = "declared matrix sha256 != pinned matrix sha256"
    elif kind == "partial-build-restart":
        count = fault["interrupt_after_placed_items"]
        if not 0 < count < len(base["base_records"]):
            raise AssertionError("restart fault must interrupt a partial build")
        derivation = (
            f"interrupt after {count} of {len(base['base_records'])} placements; "
            "recovery is a fresh staging rebuild from the same package"
        )
    elif kind == "same-duration-wrong-media":
        declared = base["declared_media"]
        if (
            fault["presented_media_sha256"] == declared["sha256"]
            or fault["presented_duration_frames"] != declared["duration_frames"]
        ):
            raise AssertionError("wrong-media fault must swap bytes at identical duration")
        derivation = (
            "presented duration equals the declared binding but the sha256 differs; "
            "only the hash binding catches the substitution"
        )
    elif kind == "false-render-complete":
        percentage = fault["reported_completion_percentage"]
        if render_complete(percentage):
            raise AssertionError("false-render-complete fault must stay below 100")
        derivation = (
            f"CompletionPercentage {percentage} != 100 so the frozen rule classifies "
            "the job incomplete regardless of the localized status string"
        )
    elif kind == "blocking-qc-privacy":
        segments = {row["segment_id"] for row in base["base_records"]}
        if fault["flagged_segment_id"] not in segments:
            raise AssertionError("privacy fault must flag a declared segment")
        derivation = (
            "deterministic QC fails closed on the declared blocking privacy flag; "
            "only a human dismissal may clear it"
        )
    else:
        raise AssertionError(f"unknown fault kind: {kind}")
    return route, derivation


def derive_fixture(fixture_id):
    manifest = load_manifest(fixture_id)
    base = manifest["base"]
    route, derivation = derive_route(fixture_id, manifest)
    return {
        "derived_from": base["derived_from"],
        "base_records": base["base_records"],
        "declared_media": base["declared_media"],
        "fault": manifest["fault"],
        "expected_route": route,
        "route_derivation": derivation,
        "package": derive_package(base),
    }


def derive():
    import json

    return {
        "fixtures": {name: derive_fixture(name) for name in FIXTURE_IDS},
        "pinned": {
            "capability_matrix_path": PINNED_MATRIX_PATH,
            "capability_matrix_sha256": PINNED_MATRIX_SHA256,
            "completion_field": COMPLETION_FIELD,
            "completion_value": COMPLETION_VALUE,
            "frame_origin": FRAME_ORIGIN,
            "start_timecode": START_TIMECODE,
            "subtitle_mux_argv": list(SUBTITLE_MUX_ARGV),
        },
        "schema_version": "phase-2-golden-expected-v1",
    }


def import_audit(source):
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    forbidden = sorted(
        name for name in imports if name.split(".", maxsplit=1)[0] not in ALLOWED_IMPORT_ROOTS
    )
    return {
        "allowed_roots": sorted(ALLOWED_IMPORT_ROOTS),
        "audit_result": "pass" if not forbidden else "fail",
        "derivation_source_sha256": file_sha256(source),
        "forbidden_imports": forbidden,
        "observed_imports": sorted(imports),
        "produced_outputs_read": False,
        "stdlib_and_common_only": not forbidden,
    }


def main():
    import json

    phase_dir = Path(__file__).resolve().parent
    pipeline_root = phase_dir.parents[3]
    source = Path(__file__).resolve()
    expected = derive()
    audit = import_audit(source)
    expected_bytes = canonical_bytes(expected)
    audit_bytes = canonical_bytes(audit)
    (phase_dir / "expected.json").write_bytes(expected_bytes)
    (phase_dir / "import-audit.json").write_bytes(audit_bytes)
    manifest_hashes = {
        fixture_id: file_sha256(MANIFEST_DIR / f"{fixture_id}.json")
        for fixture_id in FIXTURE_IDS
    }
    derived_from = {
        fixture_id: load_manifest(fixture_id)["base"]["derived_from"]
        for fixture_id in FIXTURE_IDS
    }
    phase1_manifest_hashes = {
        derived_from[fixture_id]: file_sha256(
            PHASE_1_MANIFEST_DIR / f"{derived_from[fixture_id]}.json"
        )
        for fixture_id in FIXTURE_IDS
    }
    index = {
        "audit_sha256": file_sha256(phase_dir / "import-audit.json"),
        "derivation_source_sha256": file_sha256(source),
        "expected_sha256": file_sha256(phase_dir / "expected.json"),
        "fixture_ids": list(FIXTURE_IDS),
        "fixture_manifest_sha256s": manifest_hashes,
        "phase1_fixture_manifest_sha256s": phase1_manifest_hashes,
        "recipe_and_derivation_frozen": True,
        "schema_version": "golden-index-v1",
    }
    (phase_dir / "index.json").write_bytes(canonical_bytes(index))
    print(hashlib.sha256(canonical_bytes(index)).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
