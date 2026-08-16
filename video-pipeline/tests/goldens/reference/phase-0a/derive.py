from __future__ import annotations

import ast
import hashlib
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import JsonValue, canonical_bytes, file_sha256

FIXTURE_ID = "p0a-cfr30-fixed"
FRAME_RATE = Fraction(30, 1)
SOURCE_FRAME_COUNT = 600
SAMPLE_RATE = 48_000
PULSE_FRAMES = (0, 150, 300, 450)
INTRO_FRAMES = 30
OUTRO_FRAMES = 30
CUTS = ((0, 300), (300, 600))
SUBTITLE_RECORD_SPAN = (180, 240)
ALLOWED_IMPORT_ROOTS = frozenset(
    {"__future__", "ast", "common", "fractions", "hashlib", "json", "pathlib", "sys"}
)


def rational(value: Fraction) -> dict[str, int]:
    return {"den": value.denominator, "num": value.numerator}


def derive() -> JsonValue:
    frame_duration = Fraction(1, 1) / FRAME_RATE
    pulse_samples = tuple(
        int(Fraction(frame, 1) * SAMPLE_RATE / FRAME_RATE) for frame in PULSE_FRAMES
    )
    first_cut_length = CUTS[0][1] - CUTS[0][0]
    second_cut_length = CUTS[1][1] - CUTS[1][0]
    record_frame_count = INTRO_FRAMES + first_cut_length + second_cut_length + OUTRO_FRAMES
    result: dict[str, JsonValue] = {
        "audio": {
            "duration_samples": int(Fraction(SOURCE_FRAME_COUNT, 1) * SAMPLE_RATE / FRAME_RATE),
            "pulse_frames": list(PULSE_FRAMES),
            "pulse_sample_positions": list(pulse_samples),
            "sample_rate": SAMPLE_RATE,
        },
        "cuts": [
            {
                "av_link_id": "av-cut-001",
                "record_span": {"end_frame": 330, "start_frame": 30},
                "source_span": {"end_frame": 300, "start_frame": 0},
            },
            {
                "av_link_id": "av-cut-002",
                "record_span": {"end_frame": 630, "start_frame": 330},
                "source_span": {"end_frame": 600, "start_frame": 300},
            },
        ],
        "fixture_id": FIXTURE_ID,
        "frame_duration_seconds": rational(frame_duration),
        "render": {
            "audio_sample_rate": "48000",
            "duration_seconds": rational(Fraction(record_frame_count, 1) / FRAME_RATE),
            "frame_count": record_frame_count,
            "video_avg_frame_rate": "30/1",
            "video_r_frame_rate": "30/1",
        },
        "source": {
            "duration_seconds": rational(Fraction(SOURCE_FRAME_COUNT, 1) / FRAME_RATE),
            "frame_count": SOURCE_FRAME_COUNT,
            "frame_rate": rational(FRAME_RATE),
        },
        "subtitle_timing": [
            {
                "end_frame": SUBTITLE_RECORD_SPAN[1],
                "end_seconds": rational(Fraction(SUBTITLE_RECORD_SPAN[1], 1) / FRAME_RATE),
                "start_frame": SUBTITLE_RECORD_SPAN[0],
                "start_seconds": rational(Fraction(SUBTITLE_RECORD_SPAN[0], 1) / FRAME_RATE),
                "text": "PHASE 0A FIXED SUBTITLE",
            }
        ],
    }
    return result


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
    result: dict[str, JsonValue] = {
        "allowed_roots": sorted(ALLOWED_IMPORT_ROOTS),
        "audit_result": "pass" if not forbidden else "fail",
        "derivation_source_sha256": file_sha256(source),
        "forbidden_imports": forbidden,
        "observed_imports": sorted(imports),
        "produced_outputs_read": False,
        "stdlib_and_common_only": not forbidden,
    }
    return result


def main() -> int:
    phase_dir = Path(__file__).resolve().parent
    pipeline_root = phase_dir.parents[3]
    manifest = pipeline_root / "tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json"
    source = Path(__file__).resolve()
    expected = derive()
    audit = import_audit(source)
    expected_path = phase_dir / "expected.json"
    audit_path = phase_dir / "import-audit.json"
    expected_path.write_bytes(canonical_bytes(expected))
    audit_path.write_bytes(canonical_bytes(audit))
    index: JsonValue = {
        "audit_sha256": file_sha256(audit_path),
        "derivation_source_sha256": file_sha256(source),
        "expected_sha256": file_sha256(expected_path),
        "fixture_id": FIXTURE_ID,
        "fixture_manifest_sha256": file_sha256(manifest),
        "recipe_and_derivation_frozen": True,
        "schema_version": "golden-index-v1",
    }
    (phase_dir / "index.json").write_bytes(canonical_bytes(index))
    print(hashlib.sha256(canonical_bytes(index)).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
