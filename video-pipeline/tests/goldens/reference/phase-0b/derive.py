from __future__ import annotations

import ast
import hashlib
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import JsonValue, canonical_bytes, file_sha256

CFR30 = Fraction(30, 1)
CFR24 = Fraction(24, 1)
SAMPLE_RATE = 48000
CONTENT_OFFSET = 1024
ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "ast", "common", "fractions", "hashlib", "json", "pathlib", "sys"}
)
FIXTURE_IDS = (
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
)


def round_half_away(value: Fraction) -> int:
    numerator, denominator = value.numerator, value.denominator
    if denominator == 1:
        return numerator
    return (2 * numerator + denominator) // (2 * denominator)


def ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def rational(value: Fraction) -> dict[str, int]:
    return {"den": value.denominator, "num": value.numerator}


def cfr_pts(rate: Fraction, frames: int) -> list[Fraction]:
    return [Fraction(index, 1) / rate for index in range(frames)]


def vfr_pts() -> list[Fraction]:
    return [Fraction((5 * index) // 2, 60) for index in range(120)]


Conversion = tuple[int, list[int], list[int]]


def convert(pts: list[Fraction], duration: Fraction, target: Fraction) -> Conversion:
    assigned = [round_half_away(point * target) for point in pts]
    output_frames = ceil_fraction(duration * target - Fraction(1, 2))
    mapping: list[int] = []
    cursor = 0
    for tick in range(output_frames):
        while cursor + 1 < len(assigned) and assigned[cursor + 1] <= tick:
            cursor += 1
        mapping.append(cursor)
    shown = set(mapping)
    dropped = sorted(set(range(len(pts))) - shown)
    duplicated = sorted(
        {mapping[tick] for tick in range(1, output_frames) if mapping[tick] == mapping[tick - 1]}
    )
    return output_frames, dropped, duplicated


def tick_for_frame(
    pts: list[Fraction], target: Fraction, frame: int, dropped: set[int]
) -> int | None:
    if frame in dropped:
        return None
    return round_half_away(pts[frame] * target)


def anchor_tables(
    pts: list[Fraction],
    pulses: list[tuple[str, int, int]],
    drop30: set[int],
    drop24: set[int],
) -> tuple[list[JsonValue], list[JsonValue]]:
    anchors: list[JsonValue] = []
    markers: list[JsonValue] = []
    for pulse_id, frame, sample in pulses:
        tick30 = tick_for_frame(pts, CFR30, frame, drop30)
        tick24 = tick_for_frame(pts, CFR24, frame, drop24)
        anchors.append(
            {
                "cfr24_tick": tick24,
                "cfr30_tick": tick30,
                "pulse_id": pulse_id,
                "source_frame": frame,
                "source_sample": sample,
            }
        )
        markers.append(
            {
                "audio_sample": sample,
                "cfr24_frame": tick24,
                "cfr30_frame": tick30,
                "marker_id": pulse_id.replace("pulse", "marker"),
                "source_frame": frame,
                "source_time_seconds": rational(pts[frame]),
            }
        )
    return anchors, markers


def cue_tables(
    pts: list[Fraction],
    cues: list[tuple[str, int, int]],
) -> dict[str, JsonValue]:
    table: dict[str, JsonValue] = {}
    for cue_id, start, end in cues:
        table[cue_id] = {
            "cfr24": [round_half_away(pts[start] * CFR24), round_half_away(pts[end] * CFR24)],
            "cfr30": [round_half_away(pts[start] * CFR30), round_half_away(pts[end] * CFR30)],
        }
    return table


def cfr_variant(
    rate: Fraction,
    frames: int,
    pulses: list[tuple[str, int, int]],
    cues: list[tuple[str, int, int]],
    rotation: JsonValue,
    offset: int,
) -> dict[str, JsonValue]:
    pts = cfr_pts(rate, frames)
    duration = Fraction(frames, 1) / rate
    return build_variant(pts, duration, rate, pulses, cues, rotation, offset)


def conversion_table(name: str, conversion: Conversion) -> dict[str, JsonValue]:
    output_frames, dropped, duplicated = conversion
    return {
        "dropped_source_frames": dropped,
        "duplicated_source_frames": duplicated,
        "output_frames": output_frames,
        "target": name,
    }


def build_variant(
    pts: list[Fraction],
    duration: Fraction,
    rate: Fraction,
    pulses: list[tuple[str, int, int]],
    cues: list[tuple[str, int, int]],
    rotation: JsonValue,
    offset: int,
) -> dict[str, JsonValue]:
    conv30 = convert(pts, duration, CFR30)
    conv24 = convert(pts, duration, CFR24)
    anchors, markers = anchor_tables(
        pts,
        pulses,
        set(conv30[1]),
        set(conv24[1]),
    )
    return {
        "audio_anchor_ticks": anchors,
        "audio_content_offset_samples": offset,
        "cfr24": conversion_table("cfr24", conv24),
        "cfr30": conversion_table("cfr30", conv30),
        "readback_markers": markers,
        "rotation": rotation,
        "source": {
            "duration_seconds": rational(duration),
            "frame_count": len(pts),
            "frame_rate": rational(rate),
        },
        "subtitle_anchor_ticks": cue_tables(pts, cues),
    }


def derive() -> dict[str, JsonValue]:
    rotation: JsonValue = {
        "coded_height": 1080,
        "coded_width": 1920,
        "display_height": 1920,
        "display_width": 1080,
        "rotation_degrees": 90,
    }
    no_rotation: JsonValue = None
    variants: dict[str, JsonValue] = {
        "p0b-audio-offset1024": cfr_variant(
            CFR30,
            600,
            [
                ("pulse-0", 150, CONTENT_OFFSET + 240000),
                ("pulse-1", 300, CONTENT_OFFSET + 480000),
                ("pulse-2", 450, CONTENT_OFFSET + 720000),
            ],
            [("cue-a", 80, 160), ("cue-b", 400, 480)],
            no_rotation,
            CONTENT_OFFSET,
        ),
        "p0b-cfr24": cfr_variant(
            Fraction(24, 1),
            600,
            [
                ("pulse-0", 0, 0),
                ("pulse-1", 120, 240000),
                ("pulse-2", 240, 480000),
                ("pulse-3", 360, 720000),
                ("pulse-4", 480, 960000),
            ],
            [("cue-a", 80, 160), ("cue-b", 400, 480)],
            no_rotation,
            0,
        ),
        "p0b-ntsc2997": cfr_variant(
            Fraction(30000, 1001),
            600,
            [
                ("pulse-0", 0, 0),
                ("pulse-1", 150, 240240),
                ("pulse-2", 300, 480480),
                ("pulse-3", 450, 720720),
            ],
            [("cue-a", 100, 200), ("cue-b", 400, 500)],
            no_rotation,
            0,
        ),
        "p0b-ntsc5994": cfr_variant(
            Fraction(60000, 1001),
            600,
            [
                ("pulse-0", 0, 0),
                ("pulse-1", 150, 120120),
                ("pulse-2", 300, 240240),
                ("pulse-3", 450, 360360),
            ],
            [("cue-a", 100, 200), ("cue-b", 400, 500)],
            no_rotation,
            0,
        ),
        "p0b-rotate90": cfr_variant(
            CFR30,
            600,
            [
                ("pulse-0", 0, 0),
                ("pulse-1", 150, 240000),
                ("pulse-2", 300, 480000),
                ("pulse-3", 450, 720000),
            ],
            [("cue-a", 80, 160), ("cue-b", 400, 480)],
            rotation,
            0,
        ),
        "p0b-vfr-2-3-cadence": build_variant(
            vfr_pts(),
            Fraction(5, 1),
            Fraction(24, 1),
            [("pulse-0", 0, 0), ("pulse-1", 40, 80000), ("pulse-2", 80, 160000)],
            [("cue-a", 16, 32), ("cue-b", 64, 80)],
            no_rotation,
            0,
        ),
    }
    return {"schema_version": "phase-0b-golden-expected-v1", "variants": variants}


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
            pipeline_root / "tests/fixtures/manifests/phase-0b" / f"{fixture_id}.json"
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
