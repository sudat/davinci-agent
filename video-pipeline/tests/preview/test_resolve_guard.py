"""services/preview must never depend on the Resolve bridge (static + runtime guard)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PREVIEW_PACKAGE = Path("services/preview")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_bytes())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_preview_package_never_imports_resolve_bridge() -> None:
    for path in sorted(PREVIEW_PACKAGE.glob("*.py")):
        offenders = {module for module in _imported_modules(path) if "resolve_bridge" in module}
        assert not offenders, f"{path.name} imports resolve_bridge: {offenders}"


def test_preview_import_keeps_resolve_bridge_out_of_sys_modules() -> None:
    code = (
        "import sys\n"
        "import services.preview\n"
        "import services.preview.binding\n"
        "import services.preview.cli\n"
        "import services.preview.ffmpeg_cmd\n"
        "import services.preview.render\n"
        "import services.preview.tools\n"
        "import services.preview.trace\n"
        "leaked = [m for m in sys.modules if m.startswith('services.resolve_bridge')]\n"
        "print('resolve-modules=' + (','.join(leaked) or 'none'))\n"
        "raise SystemExit(1 if leaked else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resolve-modules=none" in result.stdout
