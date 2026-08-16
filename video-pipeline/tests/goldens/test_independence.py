from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services.fixtures.golden import GoldenAuditError, audit_derivation_source

PHASE_DIR = Path("tests/goldens/reference/phase-0a")


def test_derivation_imports_only_stdlib_and_reference_common() -> None:
    audit = audit_derivation_source(PHASE_DIR / "derive.py")

    assert audit.audit_result == "pass"
    assert audit.forbidden_imports == ()
    assert audit.produced_outputs_read is False


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


def test_import_audit_is_ast_based() -> None:
    source = (PHASE_DIR / "derive.py").read_text()

    assert isinstance(ast.parse(source), ast.Module)
