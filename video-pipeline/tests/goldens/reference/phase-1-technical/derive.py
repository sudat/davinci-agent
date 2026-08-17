from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import canonical_bytes, file_sha256

ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "ast", "common", "hashlib", "json", "pathlib", "sys", "typing"}
)
FIXTURE_IDS = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)
PAUSE_DELETE_THRESHOLD_FRAMES = 15
MIN_SPEECH_SCORE = 20
CONTENT_WEIGHT = 2
CLARITY_WEIGHT = 1

Segment = dict[str, Any]


def seg(
    ident: str,
    kind: str,
    text: str,
    start: int,
    end: int,
    content: int = 0,
    clarity: int = 0,
    pause_ms: int = 0,
    group: str | None = None,
) -> Segment:
    return {
        "segment_id": ident,
        "kind": kind,
        "text": text,
        "span": {"end_frame": end, "start_frame": start},
        "observed": {
            "content_score": content,
            "clarity_score": clarity,
            "pause_ms": pause_ms,
            "retake_group": group,
        },
    }


def link(vid: str, segment_id: str, v: tuple[int, int], a: tuple[int, int]) -> dict[str, Any]:
    return {
        "av_link_id": vid,
        "segment_id": segment_id,
        "video_span": {"end_frame": v[1], "start_frame": v[0]},
        "audio_span": {"end_frame": a[1], "start_frame": a[0]},
    }


def fixture_inputs() -> dict[str, dict[str, Any]]:
    return {
        "p1-ref-01-clean-ja": {
            "edit_source": {"frame_rate": "30/1", "total_frames": 600, "audio_sample_rate": 48000},
            "segments": [
                seg("s1", "speech", "こんにちは。この動画では映像編集の基本を紹介します。", 0, 150, 9, 9),
                seg("s2", "speech", "まず素材の取り込みから始めましょう。", 150, 300, 8, 8),
                seg("s3", "speech", "次にタイムラインの編集方法を説明します。", 300, 450, 9, 9),
                seg("s4", "speech", "最後に書き出しの設定を確認します。", 450, 600, 8, 9),
            ],
            "av_links": [
                link("av1", "s1", (0, 150), (0, 150)),
                link("av2", "s2", (150, 300), (150, 300)),
                link("av3", "s3", (300, 450), (300, 450)),
                link("av4", "s4", (450, 600), (450, 600)),
            ],
            "subtitles": [],
            "budget": {"max_output_frames": 600, "min_output_frames": 300},
            "must_include": [],
            "review_commands": [],
        },
        "p1-ref-02-pauses-fillers": {
            "edit_source": {"frame_rate": "30/1", "total_frames": 827, "audio_sample_rate": 48000},
            "segments": [
                seg("s1", "speech", "今日は撮影の裏側をお見せします。", 0, 150, 9, 9),
                seg("p1", "pause", "", 150, 164, pause_ms=450),
                seg("s2", "speech", "まずカメラのセッティングからです。", 164, 314, 7, 8),
                seg("f1", "filler", "えっと", 314, 344, 2, 2),
                seg("p2", "pause", "", 344, 359, pause_ms=500),
                seg("s3", "speech", "照明の調整に少し手間取りました。", 359, 509, 6, 7),
                seg("p3", "pause", "", 509, 527, pause_ms=600),
                seg("s4", "speech", "それでは本編を始めます。", 527, 677, 9, 9),
                seg("s5", "speech", "撮影のメイキングをお楽しみください。", 677, 827, 8, 9),
            ],
            "av_links": [
                link("av1", "s1", (0, 150), (0, 150)),
                link("av2", "p1", (150, 164), (150, 164)),
                link("av3", "s2", (164, 314), (164, 314)),
                link("av4", "f1", (314, 344), (314, 344)),
                link("av5", "p2", (344, 359), (344, 359)),
                link("av6", "s3", (359, 509), (359, 509)),
                link("av7", "p3", (509, 527), (509, 527)),
                link("av8", "s4", (527, 677), (527, 677)),
                link("av9", "s5", (677, 827), (677, 827)),
            ],
            "subtitles": [],
            "budget": {"max_output_frames": 700, "min_output_frames": 300},
            "must_include": [],
            "review_commands": [],
        },
        "p1-ref-03-multi-take-must-include": {
            "edit_source": {"frame_rate": "30/1", "total_frames": 1140, "audio_sample_rate": 48000},
            "segments": [
                seg("m0", "speech", "今回の旅行動画のハイライトを紹介します。", 0, 150, 9, 9),
                seg("t1a", "false_start", "まず初めに…", 150, 210, 3, 4, group="G1"),
                seg("t1b", "speech", "初めに京都の様子をお見せします。", 210, 360, 8, 9, group="G1"),
                seg("t1c", "speech", "初めに京都の様子をお見せします。", 360, 510, 9, 8, group="G1"),
                seg("t2a", "speech", "次に大阪のグルメを紹介します。", 510, 660, 7, 9, group="G2"),
                seg("t2b", "speech", "次に大阪のグルメを紹介します。", 660, 810, 8, 9, group="G2"),
                seg("m5", "speech", "ちょっと寄り道をしました。", 810, 960, 5, 6),
                seg("m7", "speech", "最後にお土産を紹介して終わります。", 960, 1140, 8, 8),
            ],
            "av_links": [
                link("av1", "m0", (0, 150), (0, 150)),
                link("av2", "t1a", (150, 210), (150, 210)),
                link("av3", "t1b", (210, 360), (210, 360)),
                link("av4", "t1c", (360, 510), (360, 510)),
                link("av5", "t2a", (510, 660), (510, 660)),
                link("av6", "t2b", (660, 810), (660, 810)),
                link("av7", "m5", (810, 960), (810, 960)),
                link("av8", "m7", (960, 1140), (960, 1140)),
            ],
            "subtitles": [],
            "budget": {"max_output_frames": 700, "min_output_frames": 300},
            "must_include": ["t2a"],
            "review_commands": [],
        },
        "p1-ref-04-linked-av-offset": {
            "edit_source": {"frame_rate": "30/1", "total_frames": 458, "audio_sample_rate": 48000},
            "segments": [
                seg("s1", "speech", "新幹線から見えた富士山です。", 0, 150, 9, 9),
                seg("s2", "speech", "京都の街並みはとても静かでした。", 150, 300, 9, 8),
                seg("s3", "speech", "大阪ではたくさん屋台を回りました。", 300, 450, 8, 9),
            ],
            "av_links": [
                link("av1", "s1", (0, 150), (8, 158)),
                link("av2", "s2", (150, 300), (158, 308)),
                link("av3", "s3", (300, 450), (308, 458)),
            ],
            "subtitles": [],
            "budget": {"max_output_frames": 450, "min_output_frames": 300},
            "must_include": [],
            "review_commands": [],
        },
        "p1-ref-05-review-mix": {
            "edit_source": {"frame_rate": "30/1", "total_frames": 600, "audio_sample_rate": 48000},
            "segments": [
                seg("s1", "speech", "このレストランは美味しいですね。", 0, 150, 9, 9),
                seg("s2", "speech", "看板メニューを二つ注文しました。", 150, 300, 8, 9),
                seg("s3", "speech", "はい、そうです。", 300, 450, 7, 8),
                seg("s4", "speech", "はい、そうです。", 450, 600, 7, 8),
            ],
            "av_links": [
                link("av1", "s1", (0, 150), (0, 150)),
                link("av2", "s2", (150, 300), (150, 300)),
                link("av3", "s3", (300, 450), (300, 450)),
                link("av4", "s4", (450, 600), (450, 600)),
            ],
            "subtitles": [
                {"segment_id": "s1", "subtitle_id": "st1", "text": "このレストランは美味しいですね。", "span": {"end_frame": 150, "start_frame": 0}},
                {"segment_id": "s2", "subtitle_id": "st2", "text": "看板メニューを二つ注文しました。", "span": {"end_frame": 300, "start_frame": 150}},
                {"segment_id": "s3", "subtitle_id": "st3", "text": "はい、そうです。", "span": {"end_frame": 450, "start_frame": 300}},
                {"segment_id": "s4", "subtitle_id": "st4", "text": "はい、そうです。", "span": {"end_frame": 600, "start_frame": 450}},
            ],
            "budget": {"max_output_frames": 600, "min_output_frames": 300},
            "must_include": [],
            "review_commands": [
                {"command_index": 1, "new_span": None, "new_text": None, "operation": "remove_segment", "target": {"item_id": "s2", "kind": "item_id"}},
                {"command_index": 2, "new_span": {"end_frame": 420, "start_frame": 300}, "new_text": None, "operation": "adjust_source_span", "target": {"item_id": "s3", "kind": "item_id"}},
                {"command_index": 3, "new_span": None, "new_text": "とても美味しいですね。", "operation": "correct_subtitle", "target": {"item_id": "st1", "kind": "item_id"}},
                {"command_index": 4, "new_span": None, "new_text": "はい、違います。", "operation": "correct_subtitle", "target": {"kind": "subtitle_text_match", "text": "はい、そうです。"}},
            ],
        },
    }


def score(segment: Segment) -> int:
    observed = segment["observed"]
    return CONTENT_WEIGHT * int(observed["content_score"]) + CLARITY_WEIGHT * int(observed["clarity_score"])


def span_length(span: dict[str, int]) -> int:
    return int(span["end_frame"]) - int(span["start_frame"])


def rule_decisions(segments: list[Segment], must: set[str]) -> dict[str, tuple[str, str, bool]]:
    decisions: dict[str, tuple[str, str, bool]] = {}
    groups: dict[str, list[Segment]] = {}
    for entry in segments:
        group = entry["observed"]["retake_group"]
        if entry["kind"] == "speech" and group is not None:
            groups.setdefault(str(group), []).append(entry)
    winners = set()
    for members in groups.values():
        winner = max(members, key=lambda m: (score(m), -segments.index(m)))
        winners.add(str(winner["segment_id"]))
    for entry in segments:
        ident = str(entry["segment_id"])
        kind = str(entry["kind"])
        group = entry["observed"]["retake_group"]
        if ident in must:
            decisions[ident] = ("selected", "must-include-forced", True)
        elif kind == "filler":
            decisions[ident] = ("dropped", "filler", False)
        elif kind == "false_start":
            decisions[ident] = ("dropped", "false-start", False)
        elif kind == "pause":
            if span_length(entry["span"]) >= PAUSE_DELETE_THRESHOLD_FRAMES:
                decisions[ident] = ("dropped", "long-pause-deleted", False)
            else:
                decisions[ident] = ("selected", "short-pause-retained", False)
        elif group is not None and ident not in winners:
            decisions[ident] = ("dropped", "retake-superseded", False)
        elif score(entry) < MIN_SPEECH_SCORE:
            decisions[ident] = ("dropped", "below-min-score", False)
        else:
            decisions[ident] = ("selected", "score-selected", False)
    return decisions


def enforce_budget(
    segments: list[Segment],
    decisions: dict[str, tuple[str, str, bool]],
    must: set[str],
    max_frames: int,
) -> list[str]:
    def total() -> int:
        return sum(
            span_length(entry["span"])
            for entry in segments
            if decisions[str(entry["segment_id"])][0] == "selected"
        )

    dropped: list[str] = []
    while total() > max_frames:
        droppable = [
            entry
            for entry in segments
            if decisions[str(entry["segment_id"])][0] == "selected"
            and str(entry["segment_id"]) not in must
            and str(entry["kind"]) == "speech"
        ]
        if not droppable:
            break
        victim = min(droppable, key=lambda entry: (score(entry), -segments.index(entry)))
        ident = str(victim["segment_id"])
        decisions[ident] = ("dropped", "budget-dropped", False)
        dropped.append(ident)
    return dropped


def build_plan(
    segments: list[Segment],
    decisions: dict[str, tuple[str, str, bool]],
    links: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_segment = {str(item["segment_id"]): item for item in links}
    selected = [
        entry for entry in segments if decisions[str(entry["segment_id"])][0] == "selected"
    ]
    items: list[dict[str, Any]] = []
    positions: dict[str, dict[str, Any]] = {}
    cursor = 0
    for index, entry in enumerate(selected, start=1):
        ident = str(entry["segment_id"])
        association = by_segment[ident]
        video_span = dict(association["video_span"])
        audio_span = dict(association["audio_span"])
        length = span_length(video_span)
        record = {"end_frame": cursor + length, "start_frame": cursor}
        positions[ident] = {"record": record}
        items.append(
            {
                "av_link_id": association["av_link_id"],
                "item_id": f"v{index}",
                "kind": "video",
                "segment_id": ident,
                "source_span": video_span,
                "span": record,
                "track_index": 1,
            }
        )
        items.append(
            {
                "av_link_id": association["av_link_id"],
                "item_id": f"a{index}",
                "kind": "audio",
                "segment_id": ident,
                "source_span": audio_span,
                "span": record,
                "track_index": 2,
            }
        )
        cursor += length
    for subtitle in subtitles:
        segment_id = str(subtitle["segment_id"])
        if segment_id not in positions:
            continue
        items.append(
            {
                "av_link_id": None,
                "item_id": str(subtitle["subtitle_id"]),
                "kind": "subtitle",
                "segment_id": segment_id,
                "source_span": dict(subtitle["span"]),
                "span": positions[segment_id]["record"],
                "subtitle_text": subtitle["text"],
                "track_index": 3,
            }
        )
    return items


def record_table(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in (1, 2, 3):
        cursor = 0
        for item in (entry for entry in items if int(entry["track_index"]) == track):
            length = span_length(item["span"])
            rows.append(
                {
                    "item_id": item["item_id"],
                    "kind": item["kind"],
                    "record_end": cursor + length,
                    "record_start": cursor,
                    "source_end": int(item["source_span"]["end_frame"]),
                    "source_start": int(item["source_span"]["start_frame"]),
                    "subtitle_text": item.get("subtitle_text"),
                    "track_index": track,
                }
            )
            cursor += length
    return rows


def relayout(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relaid = [dict(item) for item in items]
    for track in (1, 2, 3):
        cursor = 0
        for item in (entry for entry in relaid if int(entry["track_index"]) == track):
            length = span_length(item["source_span"])
            item["span"] = {"end_frame": cursor + length, "start_frame": cursor}
            cursor += length
    return relaid


def review_outcomes(
    commands: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    working = [dict(item) for item in items]
    for command in commands:
        target = command["target"]
        operation = str(command["operation"])
        if str(target["kind"]) == "subtitle_text_match":
            candidates = [
                item for item in working
                if item["kind"] == "subtitle" and item["subtitle_text"] == target["text"]
            ]
            if len(candidates) >= 2:
                ids = [item["item_id"] for item in candidates]
                outcomes.append(
                    {
                        "affected_item_ids": ids,
                        "classification": "ambiguous",
                        "command_index": command["command_index"],
                        "decision": "defer",
                        "operation": operation,
                        "target_candidate_ids": ids,
                    }
                )
                continue
            hit = str(candidates[0]["item_id"]) if candidates else None
        else:
            hit = str(target["item_id"])
        segment_items = [
            item for item in working if item.get("segment_id") == hit or item["item_id"] == hit
        ]
        affected = [item["item_id"] for item in segment_items]
        if operation == "remove_segment":
            working = [item for item in working if item not in segment_items]
        elif operation == "adjust_source_span":
            new_span = dict(command["new_span"])
            for item in segment_items:
                item["source_span"] = dict(new_span)
        elif operation == "correct_subtitle":
            for item in segment_items:
                if item["kind"] == "subtitle":
                    item["subtitle_text"] = command["new_text"]
        outcomes.append(
            {
                "affected_item_ids": affected,
                "classification": "clear" if affected else "unresolved",
                "command_index": command["command_index"],
                "decision": "apply" if affected else "defer",
                "operation": operation,
                "target_candidate_ids": [hit] if hit else [],
            }
        )
    return outcomes, relayout(working)


def analyzer_expectations(fixture_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    segments = spec["segments"]
    if fixture_id == "p1-ref-01-clean-ja":
        return {"fillers": [], "pauses": [], "speech_count": 4}
    if fixture_id == "p1-ref-02-pauses-fillers":
        pauses = []
        for entry in segments:
            if entry["kind"] != "pause":
                continue
            length = span_length(entry["span"])
            boundary = (
                "at"
                if length == PAUSE_DELETE_THRESHOLD_FRAMES
                else "above" if length > PAUSE_DELETE_THRESHOLD_FRAMES else "below"
            )
            pauses.append(
                {
                    "action": "delete" if length >= PAUSE_DELETE_THRESHOLD_FRAMES else "retain",
                    "boundary": boundary,
                    "pause_frames": length,
                    "pause_ms": int(entry["observed"]["pause_ms"]),
                    "segment_id": entry["segment_id"],
                }
            )
        return {
            "fillers": [
                {"segment_id": entry["segment_id"], "text": entry["text"]}
                for entry in segments
                if entry["kind"] == "filler"
            ],
            "pauses": pauses,
            "speech_below_min": [
                entry["segment_id"]
                for entry in segments
                if entry["kind"] == "speech" and score(entry) < MIN_SPEECH_SCORE
            ],
        }
    if fixture_id == "p1-ref-03-multi-take-must-include":
        groups: dict[str, list[Segment]] = {}
        for entry in segments:
            group = entry["observed"]["retake_group"]
            if entry["kind"] == "speech" and group is not None:
                groups.setdefault(str(group), []).append(entry)
        return {
            "must_include": list(spec["must_include"]),
            "must_include_missed": [],
            "retake_groups": [
                {
                    "group": group,
                    "speech_members": [str(m["segment_id"]) for m in members],
                    "winner": str(
                        max(members, key=lambda m: (score(m), -segments.index(m)))["segment_id"]
                    ),
                }
                for group, members in sorted(groups.items())
            ],
        }
    if fixture_id == "p1-ref-04-linked-av-offset":
        return {
            "conform_map": [
                {
                    "audio_span": dict(item["audio_span"]),
                    "av_link_id": item["av_link_id"],
                    "offset_frames": int(item["audio_span"]["start_frame"])
                    - int(item["video_span"]["start_frame"]),
                    "video_span": dict(item["video_span"]),
                }
                for item in spec["av_links"]
            ]
        }
    return {
        "review_sequence": [
            {"command_index": 1, "operation": "remove_segment"},
            {"command_index": 2, "operation": "adjust_source_span"},
            {"command_index": 3, "operation": "correct_subtitle"},
            {"command_index": 4, "operation": "correct_subtitle"},
        ]
    }


def derive_fixture(fixture_id: str) -> dict[str, Any]:
    spec = fixture_inputs()[fixture_id]
    segments = list(spec["segments"])
    must = set(spec["must_include"])
    decisions = rule_decisions(segments, must)
    budget_dropped = enforce_budget(
        segments, decisions, must, int(spec["budget"]["max_output_frames"])
    )
    selected = [entry for entry in segments if decisions[str(entry["segment_id"])][0] == "selected"]
    candidate_table = [
        {
            "decision": decisions[str(entry["segment_id"])][0],
            "forced": decisions[str(entry["segment_id"])][2],
            "kind": entry["kind"],
            "reason_code": decisions[str(entry["segment_id"])][1],
            "score": score(entry),
            "segment_id": entry["segment_id"],
        }
        for entry in segments
    ]
    plan_items = build_plan(segments, decisions, spec["av_links"], spec["subtitles"])
    outcomes: list[dict[str, Any]] = []
    final_items = plan_items
    if spec["review_commands"]:
        outcomes, final_items = review_outcomes(spec["review_commands"], plan_items)
    total_frames = sum(span_length(entry["span"]) for entry in selected)
    return {
        "analyzer_expectations": analyzer_expectations(fixture_id, spec),
        "candidate_table": candidate_table,
        "ir_records": record_table(plan_items),
        "plan_items": plan_items,
        "review_final_ir_records": record_table(final_items),
        "review_final_plan_items": final_items,
        "review_outcomes": outcomes,
        "selection": {
            "budget_applied": bool(budget_dropped),
            "budget_dropped": budget_dropped,
            "dropped_ids": [
                str(entry["segment_id"])
                for entry in segments
                if decisions[str(entry["segment_id"])][0] == "dropped"
            ],
            "must_include_missed": [],
            "selected_ids": [str(entry["segment_id"]) for entry in selected],
            "total_selected_frames": total_frames,
        },
    }


def transcript_spans(fixture_id: str) -> list[dict[str, Any]]:
    return [
        {
            "segment_id": entry["segment_id"],
            "text": entry["text"],
            "start_frame": int(entry["span"]["start_frame"]),
            "end_frame": int(entry["span"]["end_frame"]),
        }
        for entry in fixture_inputs()[fixture_id]["segments"]
    ]


def derive() -> dict[str, Any]:
    return {
        "fixtures": {name: derive_fixture(name) for name in FIXTURE_IDS},
        "rules": {
            "min_speech_score": MIN_SPEECH_SCORE,
            "ordering": "source-order-stable",
            "pause_delete_threshold_frames": PAUSE_DELETE_THRESHOLD_FRAMES,
            "scoring": f"{CONTENT_WEIGHT}*content+{CLARITY_WEIGHT}*clarity",
        },
        "schema_version": "phase-1-technical-golden-expected-v1",
        "transcript_spans": {name: transcript_spans(name) for name in FIXTURE_IDS},
    }


def import_audit(source: Path) -> dict[str, Any]:
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
            pipeline_root / "tests/fixtures/manifests/phase-1-technical" / f"{fixture_id}.json"
        )
        for fixture_id in FIXTURE_IDS
    }
    index: dict[str, Any] = {
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
