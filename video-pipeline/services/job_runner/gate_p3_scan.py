"""Static Builder inspection for the Phase-3 gate (Todo 62).

PRD 1657: the Builder itself must carry no Channel-specific conditions.
``scan_channel_branches`` AST-walks the declared Builder surface — the
``services/build`` tree, the Resolve adapter package, and the presentation
media/build modules — and types every branch-shaped reference to a channel
or brand identifier as a finding. The profile layer (profiles, manifest,
snapshot, registry) is deliberately OUT of scope: it is the declared,
hash-sealed swap mechanism, not the Builder. ``scan_phase4_imports``
flags any Phase-4 capability module reachable from this process.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from services.job_runner.gate_p3_models import P3ScanFinding

CHANNEL_ID_NAMES: Final = frozenset({"channel_id", "brand_id", "channel", "brand"})
BRANCH_NODES: Final = (ast.If, ast.IfExp, ast.While, ast.Compare, ast.BoolOp, ast.Match)
PHASE_4_TOKENS: Final = (
    "scene_detection",
    "motion_analysis",
    "visual_similarity",
    "shot_clustering",
    "contact_sheet_enhanced",
    "candidate_ranking",
    "ambient_preservation",
    "travel_pov",
)
PRESENTATION_BUILDER_MODULES: Final = (
    "services/presentation/parity_live_build.py",
    "services/presentation/parity_live_media.py",
    "services/presentation/overlay_live_media.py",
    "services/presentation/overlay_render.py",
    "services/presentation/audio_live_media.py",
    "services/presentation/audio_mix.py",
    "services/presentation/color_render.py",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_repo_path(relative: Path) -> Path:
    """Resolve a repo-relative path whether cwd is the repo or not."""

    return relative if relative.is_file() else repo_root() / relative


def default_channel_roots(root: Path | None = None) -> tuple[Path, ...]:
    """The declared Builder surface: build tree, adapter, presentation media."""

    base = (root or repo_root()).resolve()
    build_tree = sorted((base / "services" / "build").glob("*.py"))
    adapter_tree = sorted((base / "services" / "resolve_adapter").glob("*.py"))
    presentation = [base / relative for relative in PRESENTATION_BUILDER_MODULES]
    missing = [str(path) for path in presentation if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"declared builder module missing: {missing}")
    return tuple(build_tree + adapter_tree + presentation)


def _identifier_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _references_channel_identifier(node: ast.AST) -> bool:
    return any(
        (_identifier_of(child) or "") in CHANNEL_ID_NAMES
        for child in ast.walk(node)
        if isinstance(child, ast.Name | ast.Attribute)
    )


def _findings_for(tree: ast.Module, path: Path) -> list[P3ScanFinding]:
    findings: list[P3ScanFinding] = []
    seen_lines: set[int] = set()
    lines = path.read_bytes().splitlines()
    for node in ast.walk(tree):
        if isinstance(node, BRANCH_NODES) and _references_channel_identifier(node):
            line = getattr(node, "lineno", 1)
            if line in seen_lines:
                continue
            seen_lines.add(line)
            snippet = (
                lines[line - 1].decode(errors="replace").strip() if line <= len(lines) else ""
            )
            findings.append(
                P3ScanFinding(path=path.as_posix(), line=line, snippet=snippet[:160])
            )
    findings.sort(key=lambda finding: (finding.path, finding.line))
    return findings


def scan_channel_branches(roots: Iterable[Path]) -> tuple[P3ScanFinding, ...]:
    """Recompute channel-id branch findings over the given roots (AST-based)."""

    findings: list[P3ScanFinding] = []
    for root in roots:
        path = Path(root)
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            findings.append(
                P3ScanFinding(path=path.as_posix(), line=0, snippet="<unparseable module>")
            )
            continue
        findings.extend(_findings_for(tree, path))
    return tuple(findings)


def scan_phase4_imports(module_names: Iterable[str]) -> tuple[str, ...]:
    """Flag any loaded module whose name carries a Phase-4 capability token."""

    flagged: list[str] = []
    for name in module_names:
        lowered = name.lower()
        if any(token in lowered for token in PHASE_4_TOKENS):
            flagged.append(name)
    return tuple(sorted(set(flagged)))


def loaded_module_names() -> tuple[str, ...]:
    import sys  # noqa: PLC0415

    return tuple(sorted(sys.modules))


__all__ = [
    "BRANCH_NODES",
    "CHANNEL_ID_NAMES",
    "PHASE_4_TOKENS",
    "PRESENTATION_BUILDER_MODULES",
    "default_channel_roots",
    "loaded_module_names",
    "repo_root",
    "resolve_repo_path",
    "scan_channel_branches",
    "scan_phase4_imports",
]
