"""Compound-server AST parsing for the vendored davinci-resolve-mcp checkout.

Compound tool action sets come from the ``_unknown(action, [...])`` dispatch
registries in ``src/server.py`` (the authoritative valid-action lists the
server itself reports on UNKNOWN_ACTION), with ``action ==`` dispatch chains
as the fallback for tools without a registry; safety classes mirror the
vendor's ``_annotations_for_tool_name`` classifier with its tool-name tables
extracted statically; confirm-token guard requirements come from
``_TOKEN_GATED_DESTRUCTIVE_ACTIONS``. Malformed shapes raise
:class:`VendorSurfaceError`, never a silent default.
"""

from __future__ import annotations

import ast
from typing import Final, Literal

from services.contracts.primitives import StrictModel

_UNKNOWN_FUNCTION: Final = "_unknown"
_UNKNOWN_ARG_COUNT: Final = 2
_TOOL_ACTION_PAIR_LEN: Final = 2
_ANNOTATIONS_FUNCTION: Final = "_annotations_for_tool_name"

SafetyClass = Literal["write", "destructive", "external_write", "external_destructive"]


class VendorSurfaceError(Exception):
    """The vendor checkout cannot be parsed into a complete surface."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ServerParse(StrictModel):
    """Everything statically extractable from ``src/server.py``."""

    provider_version: str
    compound_actions: tuple[tuple[str, tuple[str, ...]], ...]
    safety_tables: dict[str, tuple[str, ...]]
    confirm_token_actions: frozenset[tuple[str, str]]
    duplicate_actions: tuple[tuple[str, str], ...]


def parse_server(source: str) -> ServerParse:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise VendorSurfaceError("server-unparsable", str(exc)) from exc
    module_values: dict[str, ast.expr] = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, (ast.List, ast.Set, ast.Tuple, ast.Call))
    }
    registered = registered_tool_functions(tree)
    if not registered:
        raise VendorSurfaceError(
            "compound-tools-missing", "src/server.py registers no @mcp.tool functions"
        )
    compound: dict[str, tuple[str, ...]] = {}
    duplicates: list[tuple[str, str]] = []
    for name, node in registered:
        actions, tool_dups = _tool_actions(node, module_values)
        if not actions:
            raise VendorSurfaceError(
                "tool-actions-unparsable",
                f"compound tool {name!r} exposes no extractable action set",
            )
        compound[name] = actions
        duplicates.extend((name, action) for action in tool_dups)
    return ServerParse(
        provider_version=_provider_version(tree),
        compound_actions=tuple(sorted(compound.items())),
        safety_tables=_annotations_tables(tree, module_values),
        confirm_token_actions=_token_gated(module_values),
        duplicate_actions=tuple(duplicates),
    )


def _provider_version(tree: ast.Module) -> str:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "VERSION"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise VendorSurfaceError("provider-version-missing", "src/server.py has no VERSION constant")


type _ToolFn = tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]


def registered_tool_functions(tree: ast.Module) -> list[_ToolFn]:
    found: list[_ToolFn] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            ):
                found.append((node.name, node))
                break
    return found


def _tool_actions(
    node: ast.FunctionDef | ast.AsyncFunctionDef, module_values: dict[str, ast.expr]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The tool's action set plus any vendor-side intra-tool duplicates."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == _UNKNOWN_FUNCTION
            and len(sub.args) >= _UNKNOWN_ARG_COUNT
        ):
            resolved = _resolve_str_list(sub.args[1], module_values)
            if resolved is not None:
                return _dedupe(resolved)
    return _dedupe(_dispatch_actions(node))


def _dispatch_actions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    return tuple(
        sub.comparators[0].value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Compare)
        and isinstance(sub.left, ast.Name)
        and sub.left.id == "action"
        and len(sub.ops) == 1
        and isinstance(sub.ops[0], ast.Eq)
        and len(sub.comparators) == 1
        and isinstance(sub.comparators[0], ast.Constant)
        and isinstance(sub.comparators[0].value, str)
    )


def _resolve_str_list(
    node: ast.expr, module_values: dict[str, ast.expr]
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        target = module_values.get(node.id)
        return None if target is None else _resolve_str_list(target, module_values)
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    resolved: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            resolved.append(element.value)
        elif isinstance(element, ast.Starred):
            starred = _resolve_str_list(element.value, module_values)
            if starred is None:
                return None
            resolved.extend(starred)
        else:
            return None
    return tuple(resolved)


def _dedupe(actions: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    seen: dict[str, int] = {}
    for action in actions:
        seen[action] = seen.get(action, 0) + 1
    unique = tuple(seen)
    duplicates = tuple(action for action, count in seen.items() if count > 1)
    return unique, duplicates


def _annotations_tables(
    tree: ast.Module, module_values: dict[str, ast.expr]
) -> dict[str, tuple[str, ...]]:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != _ANNOTATIONS_FUNCTION:
            continue
        local_strings: dict[str, tuple[str, ...]] = {}
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                resolved = _resolve_str_list(sub.value, module_values)
                if resolved is not None:
                    local_strings[sub.targets[0].id] = resolved
        external = local_strings.get("external_tools")
        destructive = local_strings.get("destructive_tools")
        if external is None or destructive is None:
            raise VendorSurfaceError(
                "safety-tables-unparsable",
                f"{_ANNOTATIONS_FUNCTION} lacks external_tools/destructive_tools lists",
            )
        return {"__external__": external, "__destructive__": destructive}
    raise VendorSurfaceError(
        "safety-tables-missing", f"src/server.py has no {_ANNOTATIONS_FUNCTION} function"
    )


def _token_gated(module_values: dict[str, ast.expr]) -> frozenset[tuple[str, str]]:
    node = module_values.get("_TOKEN_GATED_DESTRUCTIVE_ACTIONS")
    if (
        node is None
        or not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "frozenset"
        or not node.args
    ):
        raise VendorSurfaceError(
            "token-gated-missing", "src/server.py has no _TOKEN_GATED_DESTRUCTIVE_ACTIONS set"
        )
    return _token_gated_from_node(node.args[0])


def _token_gated_from_node(node: ast.expr) -> frozenset[tuple[str, str]]:
    elements: tuple[ast.expr, ...]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elements = tuple(node.elts)
    else:
        raise VendorSurfaceError("token-gated-unparsable", "unexpected set literal shape")
    pairs: set[tuple[str, str]] = set()
    for element in elements:
        if (
            isinstance(element, ast.Tuple)
            and len(element.elts) == _TOOL_ACTION_PAIR_LEN
            and isinstance(element.elts[0], ast.Constant)
            and isinstance(element.elts[0].value, str)
            and isinstance(element.elts[1], ast.Constant)
            and isinstance(element.elts[1].value, str)
        ):
            pairs.add((element.elts[0].value, element.elts[1].value))
        else:
            raise VendorSurfaceError(
                "token-gated-unparsable", "non (tool, action) pair inside token-gated set"
            )
    return frozenset(pairs)


__all__ = [
    "SafetyClass",
    "ServerParse",
    "VendorSurfaceError",
    "parse_server",
    "registered_tool_functions",
]
