"""Attack class 9: phase-4 imports are denied by the scope checker.

Controller-owned: the attacker tree is independently AST-verified to
REALLY contain the forbidden imports (the attack is real), then the
product-side ``check_scope`` must flag it; the real tree must scan clean
at phase 3 both via check_scope AND via this suite's own independent
AST walk — check_scope is a layer, never the verdict itself.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from services.policy.check_scope import (
    FORBIDDEN_IMPORT_ROOTS,
    SUPPORTED_PHASES,
    check_scope,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _attacker_tree(tmp_path: Path) -> Path:
    analyze = tmp_path / "services" / "analyze"
    analyze.mkdir(parents=True)
    (analyze / "vision_tracker.py").write_text(
        "import cv2\nimport numpy\n", encoding="utf-8"
    )
    (analyze / "face_panel_match.py").write_text(
        "from services.foundation_io import sha256_file\n", encoding="utf-8"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "travel.json").write_text('{"pov_autoselect": true}', encoding="utf-8")
    return tmp_path


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_10_attacker_imports_really_exist_then_denied(tmp_path: Path) -> None:
    root = _attacker_tree(tmp_path)
    found = _imports_of(root / "services" / "analyze" / "vision_tracker.py")
    assert "cv2" in found  # the attack is genuinely present
    violations = check_scope(root, scan_imports=True, scan_config=True)
    assert any("cv2" in violation for violation in violations)
    assert any("face" in violation for violation in violations)
    assert any("travel" in violation for violation in violations)
    assert main(["--phase", "3", "--root", str(root)]) == 1


def test_11_phase_four_feature_request_is_out_of_scope(tmp_path: Path) -> None:
    assert 4 not in SUPPORTED_PHASES
    assert main(["--phase", "4", "--root", str(tmp_path)]) == 2


def test_20_real_tree_is_clean_at_phase_3_via_independent_walk() -> None:
    analyze = REPO_ROOT / "services" / "analyze"
    assert analyze.is_dir()
    for path in sorted(analyze.rglob("*.py")):
        roots = _imports_of(path)
        assert not roots & set(FORBIDDEN_IMPORT_ROOTS), path
    assert main(["--phase", "3", "--root", str(REPO_ROOT), "--imports"]) == 0
    assert main(["--phase", "3", "--root", str(REPO_ROOT), "--config"]) == 0
    assert main(["--phase", "3", "--root", str(REPO_ROOT), "--ast", "--imports", "--config"]) == 0


def test_21_exact_token_matching_does_not_trip_on_innocent_names(tmp_path: Path) -> None:
    analyze = tmp_path / "services" / "analyze"
    analyze.mkdir(parents=True)
    (analyze / "interface.py").write_text("import json\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.json").write_text('{"interface": "surface"}', encoding="utf-8")
    assert check_scope(tmp_path, scan_imports=True, scan_config=True) == []
    assert main(["--phase", "3", "--root", str(tmp_path)]) == 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(["--phase", "3", "--root", str(REPO_ROOT)]))
