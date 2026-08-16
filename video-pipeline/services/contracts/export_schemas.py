from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from services.contracts.build_report import BuildReport0A
from services.contracts.evidence import EVIDENCE_0A_ADAPTER
from services.contracts.primitives import ArtifactEnvelope
from services.contracts.timeline_ir import TimelineIr0A
from services.foundation_io import atomic_write

DEFAULT_OUTPUT_DIRECTORY: Final = Path("schemas/contracts")


@dataclass(frozen=True, slots=True)
class SchemaDocument:
    filename: str
    payload: bytes


def _model_schema_document(filename: str, model: type[BaseModel]) -> SchemaDocument:
    schema = model.model_json_schema()
    payload = json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"
    return SchemaDocument(filename=filename, payload=payload)


def schema_documents() -> tuple[SchemaDocument, ...]:
    evidence_schema = EVIDENCE_0A_ADAPTER.json_schema()
    evidence_payload = json.dumps(
        evidence_schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"
    return (
        _model_schema_document("artifact-envelope.json", ArtifactEnvelope[str]),
        _model_schema_document("build-report-0a.json", BuildReport0A),
        SchemaDocument(filename="evidence-0a.json", payload=evidence_payload),
        _model_schema_document("timeline-ir-0a.json", TimelineIr0A),
    )


def export_schemas(output_dir: Path, *, check: bool) -> bool:
    documents = schema_documents()
    expected_names = {document.filename for document in documents}
    actual_names = (
        {path.name for path in output_dir.glob("*.json")} if output_dir.is_dir() else set()
    )
    drifted = check and actual_names != expected_names
    for document in documents:
        path = output_dir / document.filename
        if check:
            if not path.is_file() or path.read_bytes() != document.payload:
                print(f"schema drift: {path}")
                drifted = True
        else:
            atomic_write(path, document.payload)
            print(f"exported: {path}")
    if check and actual_names - expected_names:
        for extra_name in sorted(actual_names - expected_names):
            print(f"schema drift: unexpected {output_dir / extra_name}")
    if check and not drifted:
        print("schemas in sync")
    return not drifted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    return 0 if export_schemas(arguments.output_dir, check=arguments.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
