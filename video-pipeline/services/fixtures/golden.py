from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final, assert_never

from services.foundation_io import sha256_file

ALLOWED_ROOTS: Final = frozenset(
    {"__future__", "ast", "common", "fractions", "hashlib", "json", "pathlib", "sys"}
)


@dataclass(frozen=True, slots=True)
class GoldenAuditError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class GoldenAudit:
    audit_result: str
    derivation_source_sha256: str
    observed_imports: tuple[str, ...]
    forbidden_imports: tuple[str, ...]
    produced_outputs_read: bool


def audit_derivation_source(path: Path) -> GoldenAudit:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports: set[str] = set()
    produced_outputs_read = False
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                imports.update(alias.name for alias in names)
            case ast.ImportFrom(module=module) if module is not None:
                imports.add(module)
            case ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id=name),
                    attr="read_bytes" | "read_text",
                )
            ):
                if name in {
                    "expected_path",
                    "audit_path",
                    "output",
                }:
                    produced_outputs_read = True
            case ast.AST():
                continue
            case unreachable:
                assert_never(unreachable)
    forbidden = tuple(
        sorted(name for name in imports if name.split(".", maxsplit=1)[0] not in ALLOWED_ROOTS)
    )
    if forbidden:
        raise GoldenAuditError(f"forbidden Golden imports: {','.join(forbidden)}")
    if produced_outputs_read:
        raise GoldenAuditError("Golden derivation reads produced outputs")
    return GoldenAudit(
        audit_result="pass",
        derivation_source_sha256=sha256_file(path),
        observed_imports=tuple(sorted(imports)),
        forbidden_imports=(),
        produced_outputs_read=False,
    )
