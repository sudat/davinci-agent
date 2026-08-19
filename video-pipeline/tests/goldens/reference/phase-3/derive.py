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
FIXTURE_IDS = ("p3-brand-a", "p3-brand-b")
MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
PHASE2_LINEAGE_MANIFEST = Path("tests/fixtures/manifests/phase-2/p2-stale-capability.json")
EDIT_SOURCE_SHA256 = "52ff0a8610e81e5294a10efe2b27b4c28706fd9a5c6b563d3822198025d0210d"
DERIVED_FROM = "p1-ref-01-clean-ja"

ASSET_KINDS = ("intro", "logo", "outro", "overlay", "se", "tone")
AUDIO_DIFF_FIELDS = ("intro_tone_hz", "outro_tone_hz", "se_hz", "tone_hz")
AUDIO_INVARIANT_FIELDS = ("sample_rate_hz", "se_duration_ms", "tone_duration_ms")
COLOR_DIFF_FIELDS = ("accent_hex", "neutral_hex", "primary_hex")
COLOR_INVARIANT_FIELDS = ("profile_id", "working_space")
STYLE_DIFF_FIELDS = (
    "background_opacity_percent",
    "font_size_px",
    "margin_bottom_px",
    "outline_color_hex",
    "outline_width_px",
    "primary_color_hex",
)
STYLE_INVARIANT_FIELDS = ("font_family", "style_id")


def load_manifest(fixture_id):
    import json

    return json.loads((MANIFEST_DIR / f"{fixture_id}.json").read_bytes())


def editorial_projection(editorial):
    rest = {key: value for key, value in editorial.items() if key != "editorial_structure_sha256"}
    return canonical_bytes(rest)


def editorial_hash(editorial):
    return hashlib.sha256(editorial_projection(editorial)).hexdigest()


def asset_table(manifest):
    return {asset["kind"]: asset["sha256"] for asset in manifest["presentation"]["assets"]}


def diff_row(a_value, b_value):
    return {"differ": a_value != b_value, "p3-brand-a": a_value, "p3-brand-b": b_value}


def derive_diff(manifest_a, manifest_b):
    assets = {
        kind: diff_row(asset_table(manifest_a)[kind], asset_table(manifest_b)[kind])
        for kind in ASSET_KINDS
    }
    audio = {
        field: diff_row(
            manifest_a["presentation"]["audio"][field],
            manifest_b["presentation"]["audio"][field],
        )
        for field in AUDIO_DIFF_FIELDS
    }
    color = {
        field: diff_row(
            manifest_a["presentation"]["color_profile"][field],
            manifest_b["presentation"]["color_profile"][field],
        )
        for field in COLOR_DIFF_FIELDS
    }
    style = {
        field: diff_row(
            manifest_a["presentation"]["subtitle_style"][field],
            manifest_b["presentation"]["subtitle_style"][field],
        )
        for field in STYLE_DIFF_FIELDS
    }
    rows_by_dimension = {
        **{f"asset.{kind}.sha256": row for kind, row in assets.items()},
        **{f"audio.{field}": row for field, row in audio.items()},
        **{f"color.{field}": row for field, row in color.items()},
        **{f"style.{field}": row for field, row in style.items()},
    }
    declared = tuple(manifest_a["declared_diff"]["dimensions"])
    if declared != tuple(manifest_b["declared_diff"]["dimensions"]):
        raise AssertionError("declared diff dimensions differ between the manifests")
    if set(declared) != set(rows_by_dimension):
        raise AssertionError("declared diff dimensions must exactly cover the comparable fields")
    differing = {dimension for dimension, row in rows_by_dimension.items() if row["differ"]}
    if differing != set(declared):
        raise AssertionError(
            "every declared difference must differ and no undeclared difference may exist"
        )
    return {
        "asset_sha256": assets,
        "audio": audio,
        "color": color,
        "declared_dimensions": list(declared),
        "dimension_check": "all-declared-differ-no-undeclared-difference",
        "style": style,
    }


def derive_invariants(manifest_a, manifest_b):
    for field in STYLE_INVARIANT_FIELDS:
        if (
            manifest_a["presentation"]["subtitle_style"][field]
            != (manifest_b["presentation"]["subtitle_style"][field])
        ):
            raise AssertionError(f"style field must stay invariant: {field}")
    for field in COLOR_INVARIANT_FIELDS:
        if (
            manifest_a["presentation"]["color_profile"][field]
            != (manifest_b["presentation"]["color_profile"][field])
        ):
            raise AssertionError(f"color field must stay invariant: {field}")
    for field in AUDIO_INVARIANT_FIELDS:
        if (
            manifest_a["presentation"]["audio"][field]
            != (manifest_b["presentation"]["audio"][field])
        ):
            raise AssertionError(f"audio field must stay invariant: {field}")
    if manifest_a["presentation"]["placement"] != manifest_b["presentation"]["placement"]:
        raise AssertionError("placement geometry must stay invariant")
    if editorial_projection(manifest_a["editorial"]) != editorial_projection(
        manifest_b["editorial"]
    ):
        raise AssertionError("the editorial structure must be identical between A and B")
    for manifest in (manifest_a, manifest_b):
        if (
            editorial_hash(manifest["editorial"])
            != (manifest["editorial"]["editorial_structure_sha256"])
        ):
            raise AssertionError("declared editorial structure hash does not match content")
    return {
        "asset_kinds": list(ASSET_KINDS),
        "audio_invariants": {
            field: manifest_a["presentation"]["audio"][field] for field in AUDIO_INVARIANT_FIELDS
        },
        "color_invariants": {
            field: manifest_a["presentation"]["color_profile"][field]
            for field in COLOR_INVARIANT_FIELDS
        },
        "editorial_equal": True,
        "editorial_structure_sha256": manifest_a["editorial"]["editorial_structure_sha256"],
        "placement": manifest_a["presentation"]["placement"],
        "style_invariants": {
            field: manifest_a["presentation"]["subtitle_style"][field]
            for field in STYLE_INVARIANT_FIELDS
        },
    }


def derive_phase2_lineage(manifest_a):
    import json

    lineage = json.loads(PHASE2_LINEAGE_MANIFEST.read_bytes())
    base = lineage["base"]
    if base["base_records"] != manifest_a["editorial"]["base_records"]:
        raise AssertionError("editorial base records must equal the frozen phase-2 base table")
    declared_media = manifest_a["editorial"]["declared_media"]
    for field in ("duration_frames", "sha256", "source_id"):
        if declared_media[field] != base["declared_media"][field]:
            raise AssertionError(f"edit-source binding must match the phase-2 lineage: {field}")
    if declared_media["sha256"] != EDIT_SOURCE_SHA256:
        raise AssertionError("edit-source hash must equal the frozen phase-1 edit source")
    return {
        "edit_source_sha256": EDIT_SOURCE_SHA256,
        "phase2_fixture": "p2-stale-capability",
        "phase2_fixture_manifest_sha256": file_sha256(PHASE2_LINEAGE_MANIFEST),
    }


def derive():
    import json

    manifest_a = load_manifest("p3-brand-a")
    manifest_b = load_manifest("p3-brand-b")
    for manifest in (manifest_a, manifest_b):
        if manifest["editorial"]["derived_from"] != DERIVED_FROM:
            raise AssertionError("the shared editorial base derives from the Phase-1 Reference")
    return {
        "ab_diff": derive_diff(manifest_a, manifest_b),
        "fixtures": {
            fixture_id: {
                "brand_id": manifest["brand_id"],
                "derived_from": manifest["editorial"]["derived_from"],
                "editorial_structure_sha256": manifest["editorial"]["editorial_structure_sha256"],
                "assets": asset_table(manifest),
            }
            for fixture_id, manifest in (
                ("p3-brand-a", manifest_a),
                ("p3-brand-b", manifest_b),
            )
        },
        "invariants": derive_invariants(manifest_a, manifest_b),
        "pinned": derive_phase2_lineage(manifest_a),
        "schema_version": "phase-3-golden-expected-v1",
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
        fixture_id: file_sha256(MANIFEST_DIR / f"{fixture_id}.json") for fixture_id in FIXTURE_IDS
    }
    index = {
        "audit_sha256": file_sha256(phase_dir / "import-audit.json"),
        "derivation_source_sha256": file_sha256(source),
        "expected_sha256": file_sha256(phase_dir / "expected.json"),
        "fixture_ids": list(FIXTURE_IDS),
        "fixture_manifest_sha256s": manifest_hashes,
        "phase2_lineage_manifest_sha256": file_sha256(PHASE2_LINEAGE_MANIFEST),
        "recipe_and_derivation_frozen": True,
        "schema_version": "golden-index-v1",
    }
    (phase_dir / "index.json").write_bytes(canonical_bytes(index))
    print(
        json.dumps(
            {"root": str(pipeline_root), "index_sha256": file_sha256(phase_dir / "index.json")}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
