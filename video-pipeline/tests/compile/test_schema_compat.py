"""Todo 44 acceptance: Phase-0C schema compatibility through the extension.

The production IR extension must leave the frozen 0A/0C contracts byte-stable:
the exported schema set stays in sync and the 0C IR schema gains none of the
production-only nodes.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.contracts.export_schemas import export_schemas
from services.contracts.timeline_ir import TimelineIr0C, TimelineIrProduction

EXPORT_DIR = Path("schemas/contracts")


def test_exported_contract_schemas_stay_in_sync(tmp_path: Path) -> None:
    output = tmp_path / "contracts"

    assert export_schemas(output, check=False)
    assert export_schemas(output, check=True)


def test_production_schema_is_exported_and_matches_the_model() -> None:
    schema_path = EXPORT_DIR / "timeline-ir-production.json"

    assert schema_path.is_file()
    exported = json.loads(schema_path.read_bytes())
    assert exported == TimelineIrProduction.model_json_schema()


def test_phase0c_ir_schema_has_no_production_nodes() -> None:
    schema = json.dumps(TimelineIr0C.model_json_schema())

    assert "subtitle_cue" not in schema
    assert '"gap"' not in schema
    assert "style_ref" not in schema
    assert "safe_area" not in schema
