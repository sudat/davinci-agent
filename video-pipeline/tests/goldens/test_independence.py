from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.foundation_io import sha256_file as file_sha256

PHASE_DIR = Path("tests/goldens/reference/phase-0a")
PHASE_0B_DIR = Path("tests/goldens/reference/phase-0b")


def test_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


def test_phase0b_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_0B_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


def test_phase0b_frozen_audit_and_index_bindings_are_current() -> None:
    audit_payload = json.loads((PHASE_0B_DIR / "import-audit.json").read_bytes())
    index_payload = json.loads((PHASE_0B_DIR / "index.json").read_bytes())

    assert audit_payload["audit_result"] == "pass"
    assert audit_payload["derivation_source_sha256"] == file_sha256(PHASE_0B_DIR / "derive.py")
    assert index_payload["derivation_source_sha256"] == file_sha256(PHASE_0B_DIR / "derive.py")
    assert index_payload["expected_sha256"] == file_sha256(PHASE_0B_DIR / "expected.json")
    assert index_payload["audit_sha256"] == file_sha256(PHASE_0B_DIR / "import-audit.json")
    for fixture_id, manifest_hash in index_payload["fixture_manifest_sha256s"].items():
        manifest = (
            Path("tests/fixtures/manifests/phase-0b") / f"{fixture_id}.json"
        )
        assert manifest_hash == file_sha256(manifest)


def test_phase0b_expected_tables_are_frozen() -> None:
    expected = json.loads((PHASE_0B_DIR / "expected.json").read_bytes())

    variants = expected["variants"]
    assert variants["p0b-cfr24"]["cfr30"]["output_frames"] == 750
    assert variants["p0b-cfr24"]["cfr30"]["duplicated_source_frames"][0] == 1
    assert variants["p0b-ntsc2997"]["cfr30"]["output_frames"] == 601
    assert variants["p0b-ntsc5994"]["cfr30"]["dropped_source_frames"][:4] == [1, 3, 5, 7]
    assert variants["p0b-vfr-2-3-cadence"]["cfr30"]["output_frames"] == 150
    assert variants["p0b-vfr-2-3-cadence"]["cfr24"]["output_frames"] == 120
    assert variants["p0b-rotate90"]["rotation"]["display_width"] == 1080
    assert variants["p0b-audio-offset1024"]["audio_content_offset_samples"] == 1024


def test_derivation_outputs_match_frozen_expected_values() -> None:
    expected = json.loads((PHASE_DIR / "expected.json").read_bytes())

    assert expected["source"]["frame_count"] == 600
    assert expected["source"]["duration_seconds"] == {"den": 1, "num": 20}
    assert expected["audio"]["pulse_sample_positions"] == [0, 240000, 480000, 720000]
    assert expected["render"]["frame_count"] == 660
    assert expected["subtitle_timing"] == [
        {
            "end_frame": 240,
            "end_seconds": {"den": 1, "num": 8},
            "start_frame": 180,
            "start_seconds": {"den": 1, "num": 6},
            "text": "PHASE 0A FIXED SUBTITLE",
        }
    ]


def test_self_derived_golden_importing_services_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "derive.py"
    source.write_text("from services.gates import GatePolicy\n")

    with pytest.raises(GoldenAuditError, match="services"):
        audit_derivation_source(source)


def test_phase0b_self_derived_golden_importing_services_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "derive.py"
    source.write_text("import services.toolchain.models\n")

    with pytest.raises(GoldenAuditError, match="services"):
        audit_derivation_source(source)


def test_import_audit_is_ast_based() -> None:
    source = (PHASE_DIR / "derive.py").read_text()

    assert isinstance(ast.parse(source), ast.Module)
