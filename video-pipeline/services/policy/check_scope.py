"""Minimal phase scope checker (Todo 35; Todo 66 hardens this).

``check_scope --phase 1`` scans ``services/analyze`` module names, their
import statements (AST), and ``config`` JSON keys for FORBIDDEN phase-4+
analyzer markers — motion analysis, visual similarity, shot clustering,
face/plate tracking, Privacy candidate ranking, Travel/POV logic, and visual
auto-selection. Matching is exact-token (splitting names/keys on ``_``,
``-``, ``.``), so words like ``interface`` can never trip the ``face``
token. The phase-1 allowlist is simply today's tree: exit 0 when clean,
exit 1 listing every violation, exit 2 on usage errors.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Final

SUPPORTED_PHASES: Final = (1,)

FORBIDDEN_IMPORT_ROOTS: Final = (
    "cv2",
    "opencv",
    "face_recognition",
    "deepface",
    "insightface",
    "mediapipe",
    "dlib",
)
FORBIDDEN_TOKENS: Final = (
    "motion",
    "similarity",
    "cluster",
    "clustering",
    "face",
    "plate",
    "privacy",
    "travel",
    "pov",
    "autoselect",
)

_TOKEN_SPLIT: Final = re.compile(r"[_\-.]+")


def _tokens(name: str) -> set[str]:
    return {part.lower() for part in _TOKEN_SPLIT.split(name) if part}


def _has_forbidden_token(name: str) -> bool:
    return bool(_tokens(name) & set(FORBIDDEN_TOKENS))


def _import_violations(path: Path, source: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names = [node.module, *[alias.name for alias in node.names]]
        for name in names:
            root = name.split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS or _has_forbidden_token(name):
                violations.append(f"{path}: forbidden import '{name}'")
    return violations


def scan_analyze_tree(root: Path) -> list[str]:
    violations: list[str] = []
    analyze_dir = root / "services" / "analyze"
    if not analyze_dir.is_dir():
        return [f"missing analyze tree: {analyze_dir}"]
    for path in sorted(analyze_dir.rglob("*.py")):
        if _has_forbidden_token(path.stem):
            violations.append(f"{path}: forbidden analyzer module name '{path.stem}'")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as error:
            violations.append(f"{path}: unreadable ({error})")
            continue
        try:
            violations.extend(_import_violations(path, source))
        except SyntaxError as error:
            violations.append(f"{path}: unparsable ({error})")
    return violations


def _config_key_violations(path: Path, payload: object, prefix: str) -> list[str]:
    violations: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            if _has_forbidden_token(key_text):
                violations.append(f"{path}: forbidden config key '{prefix}{key_text}'")
            violations.extend(_config_key_violations(path, value, f"{prefix}{key_text}."))
    elif isinstance(payload, list):
        for item in payload:
            violations.extend(_config_key_violations(path, item, prefix))
    return violations


def scan_config_tree(root: Path) -> list[str]:
    violations: list[str] = []
    config_dir = root / "config"
    if not config_dir.is_dir():
        return [f"missing config tree: {config_dir}"]
    for path in sorted(config_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            violations.append(f"{path}: config unparsable ({error})")
            continue
        violations.extend(_config_key_violations(path, payload, ""))
    return violations


def check_scope(root: Path, *, scan_imports: bool, scan_config: bool) -> list[str]:
    violations: list[str] = []
    if scan_imports:
        violations.extend(scan_analyze_tree(root))
    if scan_config:
        violations.extend(scan_config_tree(root))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_scope")
    parser.add_argument("--phase", type=int, required=True)
    parser.add_argument("--root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ast", dest="mode", action="store_const", const="ast")
    mode.add_argument("--imports", dest="mode", action="store_const", const="imports")
    mode.add_argument("--config", dest="mode", action="store_const", const="config")
    arguments = parser.parse_args(argv)

    if arguments.phase not in SUPPORTED_PHASES:
        print(f"unsupported phase {arguments.phase}: supported = {SUPPORTED_PHASES}")
        return 2

    root = arguments.root if arguments.root is not None else Path.cwd()
    scan_imports = arguments.mode in (None, "ast", "imports")
    scan_config = arguments.mode in (None, "config")
    violations = check_scope(root, scan_imports=scan_imports, scan_config=scan_config)
    if violations:
        for violation in violations:
            print(f"scope-violation: {violation}")
        print(f"phase-{arguments.phase} scope: {len(violations)} violation(s)")
        return 1
    print(f"scope-clean: phase {arguments.phase}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
