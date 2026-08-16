from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import JsonValue, canonical_bytes, file_sha256

ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "ast", "common", "hashlib", "json", "pathlib", "sys"}
)
FIXTURE_IDS = (
    "p0c-remove-clear",
    "p0c-span-clear",
    "p0c-subtitle-clear",
    "p0c-ambiguous-two-targets",
    "p0c-locked-conflict",
)
EDIT_SOURCE = {"frame_rate_num": 30, "frame_rate_den": 1, "total_frames": 750}

Item = dict[str, JsonValue]


def item(
    ident: str,
    kind: str,
    start: int,
    end: int,
    track: int,
    link: str | None = None,
    text: str | None = None,
    locks: list[str] | None = None,
) -> Item:
    payload: Item = {
        "item_id": ident,
        "kind": kind,
        "span": {"end_frame": end, "start_frame": start},
        "track_index": track,
    }
    if link is not None:
        payload["av_link_id"] = link
    if text is not None:
        payload["subtitle_text"] = text
    if locks:
        payload["locked_fields"] = sorted(locks)
    return payload


def fixture_inputs() -> dict[str, dict[str, JsonValue]]:
    return {
        "p0c-remove-clear": {
            "command": {
                "language": "ja",
                "instruction": "2番目のセグメントを削除してください",
                "operation": "remove_segment",
                "target": {"item_id": "v2", "kind": "item_id"},
            },
            "items": [
                item("v1", "video", 0, 150, 1, link="av1"),
                item("v2", "video", 150, 300, 1, link="av2"),
                item("v3", "video", 300, 450, 1, link="av3"),
                item("a1", "audio", 0, 150, 2, link="av1"),
                item("a2", "audio", 150, 300, 2, link="av2"),
                item("a3", "audio", 300, 450, 2, link="av3"),
            ],
        },
        "p0c-span-clear": {
            "command": {
                "language": "ja",
                "instruction": "2番目のセグメントの終点を60フレーム早めてください",
                "new_span": {"end_frame": 240, "start_frame": 150},
                "operation": "adjust_source_span",
                "target": {"item_id": "v2", "kind": "item_id"},
            },
            "items": [
                item("v1", "video", 0, 150, 1, link="av1"),
                item("v2", "video", 150, 300, 1, link="av2"),
                item("a1", "audio", 0, 150, 2, link="av1"),
                item("a2", "audio", 150, 300, 2, link="av2"),
            ],
        },
        "p0c-subtitle-clear": {
            "command": {
                "language": "ja",
                "instruction": "最初の字幕を「最初のセグメントでした」に直してください",
                "new_text": "最初のセグメントでした",
                "operation": "correct_subtitle",
                "target": {"item_id": "s1", "kind": "item_id"},
            },
            "items": [
                item("v1", "video", 0, 150, 1, link="av1"),
                item("a1", "audio", 0, 150, 2, link="av1"),
                item("s1", "subtitle", 0, 150, 3, text="最初のセグメントです"),
            ],
        },
        "p0c-ambiguous-two-targets": {
            "command": {
                "language": "ja",
                "instruction": "「はい、そうです」の字幕を「はい、違います」に直してください",
                "new_text": "はい、違います",
                "operation": "correct_subtitle",
                "target": {"kind": "subtitle_text_match", "text": "はい、そうです"},
            },
            "items": [
                item("v1", "video", 0, 300, 1),
                item("s1", "subtitle", 0, 150, 3, text="はい、そうです"),
                item("s2", "subtitle", 150, 300, 3, text="はい、そうです"),
            ],
        },
        "p0c-locked-conflict": {
            "command": {
                "language": "ja",
                "instruction": "2番目のセグメントの終点を60フレーム早めてください",
                "new_span": {"end_frame": 240, "start_frame": 150},
                "operation": "adjust_source_span",
                "target": {"item_id": "v2", "kind": "item_id"},
            },
            "items": [
                item("v1", "video", 0, 150, 1, link="av1"),
                item("v2", "video", 150, 300, 1, link="av2", locks=["span"]),
                item("a1", "audio", 0, 150, 2, link="av1"),
                item("a2", "audio", 150, 300, 2, link="av2"),
            ],
        },
    }


def resolve_candidates(items: list[Item], target: dict[str, JsonValue]) -> list[str]:
    if target["kind"] == "item_id":
        wanted = str(target["item_id"])
        return [str(entry["item_id"]) for entry in items if entry["item_id"] == wanted]
    text = str(target["text"])
    return [
        str(entry["item_id"])
        for entry in items
        if entry["kind"] == "subtitle" and entry.get("subtitle_text") == text
    ]


def locks_for(entry: Item) -> set[str]:
    return set(entry.get("locked_fields", []))


def operation_lock(operation: str) -> str:
    return {"remove_segment": "order", "adjust_source_span": "span", "correct_subtitle": "text"}[
        operation
    ]


def classify(
    items: list[Item],
    operation: str,
    target: dict[str, JsonValue],
) -> tuple[list[str], str]:
    candidates = resolve_candidates(items, target)
    if len(candidates) >= 2:
        return candidates, "ambiguous"
    if not candidates:
        return candidates, "unresolved"
    by_id = {str(entry["item_id"]): entry for entry in items}
    locked = locks_for(by_id[candidates[0]])
    if operation_lock(operation) in locked:
        return candidates, "conflict"
    return candidates, "clear"


def transformed_items(
    items: list[Item],
    operation: str,
    target: dict[str, JsonValue],
    new_span: dict[str, int] | None,
    new_text: str | None,
) -> tuple[list[Item], dict[str, JsonValue]]:
    by_id = {str(entry["item_id"]): entry for entry in items}
    target_id = resolve_candidates(items, target)[0]
    head = by_id[target_id]
    kept: list[Item] = []
    removed: list[str] = []
    if operation == "remove_segment":
        link = head.get("av_link_id")
        for entry in items:
            drop = entry["item_id"] == target_id or (
                link is not None and entry.get("av_link_id") == link
            )
            if drop:
                removed.append(str(entry["item_id"]))
            else:
                kept.append(dict(entry))
        return kept, {"removed_item_ids": removed}
    old_span = head["span"]
    for entry in items:
        candidate = dict(entry)
        if operation == "adjust_source_span":
            same_group = (
                candidate.get("av_link_id") is not None
                and head.get("av_link_id") is not None
                and candidate.get("av_link_id") == head.get("av_link_id")
                and candidate["span"] == old_span
            )
            if candidate["item_id"] == target_id or same_group:
                candidate["span"] = {
                    "end_frame": new_span["end_frame"],
                    "start_frame": new_span["start_frame"],
                }
        if operation == "correct_subtitle" and candidate["item_id"] == target_id:
            candidate["subtitle_text"] = new_text
        kept.append(candidate)
    return kept, {}


def record_table(items: list[Item]) -> list[dict[str, JsonValue]]:
    tracks = sorted({int(entry["track_index"]) for entry in items})
    rows: list[dict[str, JsonValue]] = []
    for track in tracks:
        cursor = 0
        for entry in items:
            if int(entry["track_index"]) != track:
                continue
            span = entry["span"]
            length = int(span["end_frame"]) - int(span["start_frame"])
            rows.append(
                {
                    "item_id": entry["item_id"],
                    "kind": entry["kind"],
                    "record_end": cursor + length,
                    "record_start": cursor,
                    "source_end": int(span["end_frame"]),
                    "source_start": int(span["start_frame"]),
                    "subtitle_text": entry.get("subtitle_text"),
                    "track_index": track,
                }
            )
            cursor += length
    return rows


def derive_fixture(name: str) -> dict[str, JsonValue]:
    spec = fixture_inputs()[name]
    items = list(spec["items"])
    command = dict(spec["command"])
    target = dict(command["target"])
    operation = str(command["operation"])
    new_span_raw = command.get("new_span")
    new_span = (
        {"end_frame": int(new_span_raw["end_frame"]), "start_frame": int(new_span_raw["start_frame"])}
        if isinstance(new_span_raw, dict)
        else None
    )
    candidates, classification = classify(items, operation, target)
    clear = classification == "clear"
    decision = "apply" if clear else "defer"
    plan_items = items
    changes: dict[str, JsonValue] = {"removed_item_ids": [], "span_changes": [], "text_changes": []}
    if clear:
        plan_items, applied = transformed_items(
            items, operation, target, new_span, command.get("new_text")
        )
        changes.update(applied)
    return {
        "classification": classification,
        "decision": decision,
        "plan_items": [
            {
                "av_link_id": entry.get("av_link_id"),
                "item_id": entry["item_id"],
                "kind": entry["kind"],
                "span": entry["span"],
                "subtitle_text": entry.get("subtitle_text"),
                "track_index": entry["track_index"],
            }
            for entry in plan_items
        ],
        "record_table": record_table(plan_items),
        "resulting_plan_version": "v2" if clear else "v1",
        "target_candidate_item_ids": candidates,
    }


def derive() -> dict[str, JsonValue]:
    fixtures = {name: derive_fixture(name) for name in FIXTURE_IDS}
    return {
        "edit_source": EDIT_SOURCE,
        "fixtures": fixtures,
        "schema_version": "phase-0c-golden-expected-v1",
    }


def import_audit(source: Path) -> JsonValue:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports: set[str] = set()
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


def main() -> int:
    phase_dir = Path(__file__).resolve().parent
    pipeline_root = phase_dir.parents[3]
    source = Path(__file__).resolve()
    expected = derive()
    audit = import_audit(source)
    expected_path = phase_dir / "expected.json"
    audit_path = phase_dir / "import-audit.json"
    expected_path.write_bytes(canonical_bytes(expected))
    audit_path.write_bytes(canonical_bytes(audit))
    manifest_hashes = {
        fixture_id: file_sha256(
            pipeline_root / "tests/fixtures/manifests/phase-0c" / f"{fixture_id}.json"
        )
        for fixture_id in FIXTURE_IDS
    }
    index: JsonValue = {
        "audit_sha256": file_sha256(audit_path),
        "derivation_source_sha256": file_sha256(source),
        "expected_sha256": file_sha256(expected_path),
        "fixture_ids": list(FIXTURE_IDS),
        "fixture_manifest_sha256s": manifest_hashes,
        "recipe_and_derivation_frozen": True,
        "schema_version": "golden-index-v1",
    }
    (phase_dir / "index.json").write_bytes(canonical_bytes(index))
    print(hashlib.sha256(canonical_bytes(index)).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
