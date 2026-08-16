from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from services.contracts.export_schemas import export_schemas


def test_schema_export_writes_canonical_files_when_target_is_empty(tmp_path: Path) -> None:
    output = tmp_path / "contracts"

    in_sync = export_schemas(output, check=False)

    assert in_sync
    for schema_path in output.glob("*.json"):
        parsed = json.loads(schema_path.read_bytes())
        expected = json.dumps(
            parsed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        assert schema_path.read_bytes() == expected + b"\n"


def test_schema_check_passes_when_generated_files_are_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "contracts"
    export_schemas(output, check=False)

    in_sync = export_schemas(output, check=True)

    assert in_sync


def test_schema_drift_is_detected_when_copy_is_tampered(tmp_path: Path) -> None:
    output = tmp_path / "contracts"
    export_schemas(output, check=False)
    schema_path = next(output.glob("*.json"))
    schema_path.write_text("{}\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.contracts.export_schemas",
            "--check",
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "schema drift" in result.stdout
