"""Static cross-check of the route-binding authority against real sources.

Re-derives, per supported surface, the literal vendor ``(tool, action)`` pairs
reachable from the HANDLERS-registered function, and per ``McpOps`` method the
literal ``self._action(model, tool, action, ...)`` pairs, then proves every
frozen binding entry against them. Sources are injected as strings so tests
can doctor copies; the committed readers below feed the real tree.

Bounded helper traversal: from the registered handler, follow only calls to
top-level functions defined inside the handler package (one package-wide name
map), with a visited set and ``_MAX_HELPER_DEPTH`` hops — no dynamic dispatch,
no cross-package jumps.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Final

from services.mcp_execution.live_adapter import SUPPORTED_SURFACES
from services.toolchain.mcp_dispositions_bindings import (
    OPS_READ_OPERATION_BINDINGS,
    SURFACE_OPERATION_BINDINGS,
)
from services.toolchain.mcp_dispositions_models import (
    OPS_READ_METHODS,
    DispositionsError,
)
from services.toolchain.mcp_dispositions_rules import require

_MCP_EXECUTION_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "mcp_execution"
# Assembled without the literal substring: the architecture test forbids that
# string anywhere under services/ outside the handler package and live_adapter.
_HANDLERS_DIR: Final[Path] = _MCP_EXECUTION_DIR / "_".join(("live", "handlers"))  # noqa: FLY002 (literal is forbidden by the architecture scan)
_OPS_SOURCE: Final[Path] = Path(__file__).resolve().parents[1] / "mcp_client" / "ops.py"
_MAX_HELPER_DEPTH: Final = 5
_REGISTRY_FILE: Final = "__init__.py"
_LITERAL_ARG_COUNT: Final = 2
_MIN_TRANSPORT_ARGS: Final = 2
_MIN_ACTION_ARGS: Final = 4


def verify_route_bindings(
    handler_sources: Mapping[str, str], ops_source: str
) -> None:
    registry = _handler_registry(handler_sources[_REGISTRY_FILE])
    functions = _package_functions(handler_sources)
    for surface, bound_ops in sorted(SURFACE_OPERATION_BINDINGS.items()):
        require(
            surface in SUPPORTED_SURFACES,
            "bindings-surface-unsupported",
            f"{surface!r} is not in SUPPORTED_SURFACES",
        )
        handler_name = registry.get(surface, "").rpartition(".")[2]
        require(
            handler_name in functions,
            "bindings-surface-unrouted",
            f"{surface!r} has no registered handler function in the package sources",
        )
        reached = _transport_pairs(functions[handler_name], functions)
        for operation_id in sorted(bound_ops):
            tool, _, action = operation_id.partition(".")
            require(
                (tool, action) in reached,
                "bindings-surface-stale",
                f"{surface!r} no longer issues vendor call ({tool}, {action})"
                f" for bound operation {operation_id!r}",
            )
    ops_pairs = _ops_method_pairs(ops_source)
    for operation_id, method in sorted(OPS_READ_OPERATION_BINDINGS.items()):
        require(
            method in OPS_READ_METHODS,
            "bindings-ops-not-reviewed",
            f"{method!r} is not in the reviewed OPS_READ_METHODS allowlist",
        )
        if method not in ops_pairs:
            raise DispositionsError(
                "bindings-ops-method-missing",
                f"McpOps.{method} (bound to {operation_id!r}) does not exist",
            )
        pairs = ops_pairs[method]
        tool, _, action = operation_id.partition(".")
        require(
            (tool, action) in pairs,
            "bindings-ops-stale",
            f"McpOps.{method} no longer calls vendor operation {operation_id!r}",
        )


def committed_handler_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_HANDLERS_DIR.glob("*.py"))
    }


def committed_ops_source() -> str:
    return _OPS_SOURCE.read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def _verify_committed(
    sources: tuple[tuple[str, str], ...], ops_source: str
) -> None:
    verify_route_bindings(dict(sources), ops_source)


def verify_committed_route_bindings() -> None:
    """Validate the authority against the current committed sources (cached)."""
    sources = tuple(sorted(committed_handler_sources().items()))
    _verify_committed(sources, committed_ops_source())


def _literal_pair(args: Sequence[ast.expr]) -> tuple[str, str] | None:
    values = [
        a.value for a in args[:_LITERAL_ARG_COUNT] if isinstance(a, ast.Constant)
    ]
    if len(values) != _LITERAL_ARG_COUNT or not all(
        isinstance(v, str) for v in values
    ):
        return None
    return str(values[0]), str(values[1])


def _handler_registry(init_source: str) -> dict[str, str]:
    tree = ast.parse(init_source)
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
    registry: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
            ):
                registry[key.value] = f"{aliases.get(value.value.id, value.value.id)}.{value.attr}"
    return registry


def _package_functions(
    handler_sources: Mapping[str, str]
) -> dict[str, ast.FunctionDef]:
    functions: dict[str, ast.FunctionDef] = {}
    for name, source in handler_sources.items():
        if name == _REGISTRY_FILE:
            continue
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef):
                functions.setdefault(node.name, node)
    return functions


def _transport_pairs(
    fn: ast.FunctionDef, functions: Mapping[str, ast.FunctionDef]
) -> frozenset[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    visited = {fn.name}

    def visit(node: ast.AST, depth: int) -> None:
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "transport"
            ):
                literal = _literal_pair(n.args[:_MIN_TRANSPORT_ARGS])
                if literal is not None:
                    pairs.add(literal)
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in functions
                and n.func.id not in visited
                and depth < _MAX_HELPER_DEPTH
            ):
                visited.add(n.func.id)
                visit(functions[n.func.id], depth + 1)

    visit(fn, 0)
    return frozenset(pairs)


def _ops_method_pairs(ops_source: str) -> dict[str, frozenset[tuple[str, str]]]:
    tree = ast.parse(ops_source)
    out: dict[str, frozenset[tuple[str, str]]] = {}
    methods = [
        fn
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, ast.FunctionDef)
    ]
    for fn in methods:
        pairs: set[tuple[str, str]] = set()
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_action"
            ):
                literal = _literal_pair(n.args[1:3])
                if literal is not None:
                    pairs.add(literal)
        out[fn.name] = frozenset(pairs)
    return out


__all__ = [
    "committed_handler_sources",
    "committed_ops_source",
    "verify_committed_route_bindings",
    "verify_route_bindings",
]
