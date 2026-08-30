"""Static vendor-surface assembly for the pinned davinci-resolve-mcp checkout.

The pinned server is the capability authority, so the inventory is built from
vendor TRUTH, not inference: the compound server parse (see
:mod:`services.toolchain.mcp_vendor_server`), granular tool registrations in
``src/granular/``, and the guarded kernel catalog from ``docs/kernels/README.md``
(the vendor's canonical kernel coverage document — the same stated-count
extraction the vendor's own release tooling uses). Missing or malformed
catalog data raises :class:`VendorSurfaceError`, never a silent default.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

from services.contracts.primitives import StrictModel
from services.toolchain.mcp_vendor_server import (
    SafetyClass,
    ServerParse,
    VendorSurfaceError,
    parse_server,
    registered_tool_functions,
)

KERNEL_README_RELPATH: Final = Path("docs/kernels/README.md")
SERVER_RELPATH: Final = Path("src/server.py")
GRANULAR_DIR_RELPATH: Final = Path("src/granular")
_KERNEL_COVERAGE_RE: Final = re.compile(
    r"Current kernel coverage:\s*\*\*(\d+) actions\*\* across \*\*(\d+) compound"
)
_KERNEL_ROW_RE: Final = re.compile(r"^\|\s*([^|]+?)\s*\|\s*`([a-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|$")
_SKIP_GRANULAR_MODULES: Final = frozenset({"__init__", "common"})


class KernelRow(StrictModel):
    kernel: str
    tool: str
    action: str


class KernelCatalog(StrictModel):
    stated_actions: int
    stated_tools: int
    rows: tuple[KernelRow, ...]


class VendorCompoundTool(StrictModel):
    name: str
    actions: tuple[str, ...]
    safety: SafetyClass


class VendorSurface(StrictModel):
    provider_version: str
    compound_tools: tuple[VendorCompoundTool, ...]
    granular_modules: tuple[tuple[str, tuple[str, ...]], ...]
    kernel_catalog: KernelCatalog
    confirm_token_actions: tuple[tuple[str, str], ...]
    duplicate_actions: tuple[tuple[str, str], ...] = ()


def parse_vendor_surface(clone_dir: Path) -> VendorSurface:
    """Parse the vendor checkout's server, granular modules, and kernel docs."""
    server = parse_server(_read_server(clone_dir / SERVER_RELPATH))
    granular = _parse_granular(clone_dir / GRANULAR_DIR_RELPATH)
    kernel = _parse_kernel_catalog(clone_dir / KERNEL_README_RELPATH)
    return VendorSurface(
        provider_version=server.provider_version,
        compound_tools=tuple(
            VendorCompoundTool(
                name=name, actions=actions, safety=_classify_safety(name, server)
            )
            for name, actions in server.compound_actions
        ),
        granular_modules=tuple((module, tuple(v)) for module, v in sorted(granular.items())),
        kernel_catalog=kernel,
        confirm_token_actions=tuple(sorted(server.confirm_token_actions)),
        duplicate_actions=tuple(sorted(server.duplicate_actions)),
    )


def _read_server(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VendorSurfaceError("server-unreadable", str(path)) from exc


def _classify_safety(tool_name: str, server: ServerParse) -> SafetyClass:
    if tool_name == "media_analysis":
        return "external_write"
    if tool_name in server.safety_tables["__external__"]:
        return "external_destructive"
    if tool_name in server.safety_tables["__destructive__"]:
        return "destructive"
    return "write"


def _parse_granular(granular_dir: Path) -> dict[str, tuple[str, ...]]:
    if not granular_dir.is_dir():
        raise VendorSurfaceError("granular-missing", f"{granular_dir} is absent")
    modules: dict[str, tuple[str, ...]] = {}
    for path in sorted(granular_dir.glob("*.py")):
        if path.stem in _SKIP_GRANULAR_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            raise VendorSurfaceError("granular-unparsable", f"{path.name}: {exc}") from exc
        names = [name for name, _ in registered_tool_functions(tree)]
        if not names:
            raise VendorSurfaceError(
                "granular-module-empty", f"{path.name} registers no @mcp.tool functions"
            )
        modules[path.stem] = tuple(sorted(dict.fromkeys(names)))
    if not modules:
        raise VendorSurfaceError("granular-missing", "no granular tool modules found")
    return modules


def _parse_kernel_catalog(readme_path: Path) -> KernelCatalog:
    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VendorSurfaceError("kernel-catalog-unreadable", str(readme_path)) from exc
    stated = _KERNEL_COVERAGE_RE.search(text)
    if stated is None:
        raise VendorSurfaceError(
            "kernel-catalog-unparsable",
            "docs/kernels/README.md lacks the 'Current kernel coverage' statement",
        )
    rows: list[KernelRow] = []
    for line in text.splitlines():
        match = _KERNEL_ROW_RE.match(line)
        if match is None:
            continue
        kernel, tool, cell = match.groups()
        if set(tool) <= {"-"} or cell.startswith(":-") or "Actions" in cell:
            continue
        for action in cell.split(","):
            name = action.strip().strip("`")
            if name:
                rows.append(KernelRow(kernel=kernel.strip(), tool=tool, action=name))
    if not rows:
        raise VendorSurfaceError("kernel-catalog-empty", "kernel coverage table has no rows")
    return KernelCatalog(
        stated_actions=int(stated.group(1)),
        stated_tools=int(stated.group(2)),
        rows=tuple(sorted(rows, key=lambda row: (row.tool, row.action, row.kernel))),
    )


__all__ = [
    "KernelCatalog",
    "KernelRow",
    "VendorCompoundTool",
    "VendorSurface",
    "VendorSurfaceError",
    "parse_vendor_surface",
]
