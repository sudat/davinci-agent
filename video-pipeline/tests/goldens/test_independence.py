from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.foundation_io import sha256_file as file_sha256

PHASE_DIR = Path("tests/goldens/reference/phase-0a")
PHASE_0B_DIR = Path("tests/goldens/reference/phase-0b")
PHASE_0C_DIR = Path("tests/goldens/reference/phase-0c")
PHASE_1_DIR = Path("tests/goldens/reference/phase-1-technical")
PHASE_2_DIR = Path("tests/goldens/reference/phase-2")


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


def test_phase0c_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_0C_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


def test_phase0c_frozen_audit_and_index_bindings_are_current() -> None:
    audit_payload = json.loads((PHASE_0C_DIR / "import-audit.json").read_bytes())
    index_payload = json.loads((PHASE_0C_DIR / "index.json").read_bytes())

    assert audit_payload["audit_result"] == "pass"
    assert audit_payload["derivation_source_sha256"] == file_sha256(PHASE_0C_DIR / "derive.py")
    assert index_payload["derivation_source_sha256"] == file_sha256(PHASE_0C_DIR / "derive.py")
    assert index_payload["expected_sha256"] == file_sha256(PHASE_0C_DIR / "expected.json")
    assert index_payload["audit_sha256"] == file_sha256(PHASE_0C_DIR / "import-audit.json")
    for fixture_id, manifest_hash in index_payload["fixture_manifest_sha256s"].items():
        manifest = Path("tests/fixtures/manifests/phase-0c") / f"{fixture_id}.json"
        assert manifest_hash == file_sha256(manifest)


def test_phase0c_expected_tables_are_frozen() -> None:
    expected = json.loads((PHASE_0C_DIR / "expected.json").read_bytes())

    fixtures = expected["fixtures"]
    assert fixtures["p0c-remove-clear"]["classification"] == "clear"
    remove_rows = fixtures["p0c-remove-clear"]["record_table"]
    assert [row["item_id"] for row in remove_rows] == ["v1", "v3", "a1", "a3"]
    assert remove_rows[1] == {
        "item_id": "v3",
        "kind": "video",
        "record_end": 300,
        "record_start": 150,
        "source_end": 450,
        "source_start": 300,
        "subtitle_text": None,
        "track_index": 1,
    }
    assert fixtures["p0c-span-clear"]["plan_items"][3]["span"] == {
        "end_frame": 240,
        "start_frame": 150,
    }
    assert fixtures["p0c-subtitle-clear"]["plan_items"][2]["subtitle_text"] == (
        "最初のセグメントでした"
    )
    assert fixtures["p0c-ambiguous-two-targets"]["classification"] == "ambiguous"
    assert fixtures["p0c-ambiguous-two-targets"]["decision"] == "defer"
    assert fixtures["p0c-ambiguous-two-targets"]["resulting_plan_version"] == "v1"
    assert fixtures["p0c-locked-conflict"]["classification"] == "conflict"
    assert fixtures["p0c-locked-conflict"]["decision"] == "defer"


def test_phase0c_self_derived_golden_importing_services_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "derive.py"
    source.write_text("from services.compile.phase0c import compile_plan\n")

    with pytest.raises(GoldenAuditError, match="services"):
        audit_derivation_source(source)


def test_phase1_self_derived_golden_importing_services_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "derive.py"
    source.write_text(
        "from services.fixtures.prepare_phase1_technical "
        "import prepare_phase1_technical\n"
    )

    with pytest.raises(GoldenAuditError, match="services"):
        audit_derivation_source(source)


def test_phase1_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_1_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


def test_phase1_frozen_audit_and_index_bindings_are_current() -> None:
    audit_payload = json.loads((PHASE_1_DIR / "import-audit.json").read_bytes())
    index_payload = json.loads((PHASE_1_DIR / "index.json").read_bytes())

    assert audit_payload["audit_result"] == "pass"
    assert audit_payload["derivation_source_sha256"] == file_sha256(PHASE_1_DIR / "derive.py")
    assert index_payload["derivation_source_sha256"] == file_sha256(PHASE_1_DIR / "derive.py")
    assert index_payload["expected_sha256"] == file_sha256(PHASE_1_DIR / "expected.json")
    assert index_payload["audit_sha256"] == file_sha256(PHASE_1_DIR / "import-audit.json")
    for fixture_id, manifest_hash in index_payload["fixture_manifest_sha256s"].items():
        manifest = Path("tests/fixtures/manifests/phase-1-technical") / f"{fixture_id}.json"
        assert manifest_hash == file_sha256(manifest)


def test_phase1_expected_tables_are_frozen() -> None:
    expected = json.loads((PHASE_1_DIR / "expected.json").read_bytes())

    fixtures = expected["fixtures"]
    assert [row["segment_id"] for row in fixtures["p1-ref-01-clean-ja"]["candidate_table"]] == [
        "s1",
        "s2",
        "s3",
        "s4",
    ]
    assert fixtures["p1-ref-01-clean-ja"]["selection"]["total_selected_frames"] == 600
    pause_rows = fixtures["p1-ref-02-pauses-fillers"]["analyzer_expectations"]["pauses"]
    assert [(row["boundary"], row["action"]) for row in pause_rows] == [
        ("below", "retain"),
        ("at", "delete"),
        ("above", "delete"),
    ]
    selection_03 = fixtures["p1-ref-03-multi-take-must-include"]["selection"]
    assert "t2a" in selection_03["selected_ids"]
    assert selection_03["budget_applied"] is True
    assert selection_03["budget_dropped"] == ["m7"]
    assert fixtures["p1-ref-04-linked-av-offset"]["analyzer_expectations"]["conform_map"][0][
        "offset_frames"
    ] == 8
    outcomes_05 = fixtures["p1-ref-05-review-mix"]["review_outcomes"]
    assert [(o["classification"], o["decision"]) for o in outcomes_05] == [
        ("clear", "apply"),
        ("clear", "apply"),
        ("clear", "apply"),
        ("ambiguous", "defer"),
    ]


def test_import_audit_is_ast_based() -> None:
    source = (PHASE_DIR / "derive.py").read_text()

    assert isinstance(ast.parse(source), ast.Module)


def test_phase2_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_2_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


def test_phase2_frozen_audit_and_index_bindings_are_current() -> None:
    audit_payload = json.loads((PHASE_2_DIR / "import-audit.json").read_bytes())
    index_payload = json.loads((PHASE_2_DIR / "index.json").read_bytes())

    assert audit_payload["audit_result"] == "pass"
    assert audit_payload["derivation_source_sha256"] == file_sha256(PHASE_2_DIR / "derive.py")
    assert index_payload["derivation_source_sha256"] == file_sha256(PHASE_2_DIR / "derive.py")
    assert index_payload["expected_sha256"] == file_sha256(PHASE_2_DIR / "expected.json")
    assert index_payload["audit_sha256"] == file_sha256(PHASE_2_DIR / "import-audit.json")
    for fixture_id, manifest_hash in index_payload["fixture_manifest_sha256s"].items():
        manifest = Path("tests/fixtures/manifests/phase-2") / f"{fixture_id}.json"
        assert manifest_hash == file_sha256(manifest)
    for p1_id, manifest_hash in index_payload["phase1_fixture_manifest_sha256s"].items():
        manifest = Path("tests/fixtures/manifests/phase-1-technical") / f"{p1_id}.json"
        assert manifest_hash == file_sha256(manifest)


def test_phase2_expected_tables_are_frozen() -> None:
    expected = json.loads((PHASE_2_DIR / "expected.json").read_bytes())

    fixtures = expected["fixtures"]
    stale = fixtures["p2-stale-capability"]
    assert stale["derived_from"] == "p1-ref-01-clean-ja"
    assert stale["expected_route"]["failure_code"] == "capability-matrix-stale"
    assert stale["expected_route"]["human_route"] == "refresh-capability-matrix"
    restart = fixtures["p2-partial-build-restart"]
    assert restart["fault"]["interrupt_after_placed_items"] == 3
    assert restart["expected_route"]["retry"] == "clean-rebuild-restart"
    wrong_media = fixtures["p2-same-duration-wrong-media"]
    assert wrong_media["expected_route"]["failure_code"] == "media-hash-drift"
    false_render = fixtures["p2-false-render-complete"]
    assert false_render["fault"]["reported_completion_percentage"] == 99
    assert false_render["expected_route"]["render"] == "refused-incomplete"
    privacy = fixtures["p2-blocking-qc-privacy"]
    assert privacy["expected_route"]["human_route"] == "privacy-dismissal-required"
    assert privacy["package"]["subtitle_step"]["mux_operation"] == "ffmpeg-mov-text"
    assert expected["pinned"]["frame_origin"] == 108000
    assert expected["pinned"]["completion_value"] == 100


def test_phase2_self_derived_golden_importing_services_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "derive.py"
    source.write_text("from services.resolve_adapter.package import compile_resolve_package\n")

    with pytest.raises(GoldenAuditError, match="services"):
        audit_derivation_source(source)
