"""Task 9 — pinned MCP operation inventory contract.

Fixtures are fully synthetic vendor trees (tmp_path, no network, no server
spawn): a mini ``src/server.py`` with the real registration shapes (``_unknown``
registries, module-level action lists with starred spreads, dispatch-chain-only
tools, duplicate action entries, the annotations classifier, the confirm-token
set), granular modules, and a ``docs/kernels/README.md`` catalog — all sized to
the pinned drift guards (35 / 353 / 136) so drift fixtures can mutate one knob
at a time. Typed failures are asserted by error code, never by silent skips.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from services.cli.mcp_inventory import (
    InventoryCommandError,
    _check_advanced,
    _check_head,
    generate_inventory,
    write_inventory_tree,
)
from services.mcp_execution.live_adapter import SUPPORTED_SURFACES
from services.release.manifest import MANIFEST_NAME, build_manifest
from services.toolchain.mcp_coverage import (
    EXPECTED_COMPOUND_TOOLS,
    EXPECTED_GRANULAR_TOOLS,
    EXPECTED_KERNEL_ACTIONS,
    LiveTool,
    McpCoverageDriftError,
    PinFacts,
    build_inventory,
    inventory_bytes,
    schema_sha256,
)
from services.toolchain.mcp_coverage_models import (
    INVENTORY_SCHEMA,
    CompoundToolRow,
    CrossCheckFacts,
    GranularToolRow,
    InventoryCounts,
    McpInventoryV1,
    OperationRow,
)
from services.toolchain.mcp_dispositions import (
    dispositions_bytes as dispositions_canonical_bytes,
)
from services.toolchain.mcp_dispositions import (
    load_dispositions,
    validate_dispositions_files,
)
from services.toolchain.mcp_dispositions_bindings import (
    OPS_READ_OPERATION_BINDINGS,
    SURFACE_OPERATION_BINDINGS,
)
from services.toolchain.mcp_dispositions_models import (
    OPS_READ_METHODS,
    DispositionRow,
    DispositionsError,
    DispositionsParityError,
)
from services.toolchain.mcp_dispositions_static import (
    committed_handler_sources,
    committed_ops_source,
    verify_route_bindings,
)
from services.toolchain.mcp_pin import McpPinError, load_mcp_pin
from services.toolchain.mcp_vendor_surface import (
    VendorSurfaceError,
    parse_vendor_surface,
)

COMMIT = "c8fbe1887324de9d897e6036efcde60417e33e8c"
OTHER_SHA = "1111111111111111111111111111111111111111"

SERVER_TEMPLATE = """\
VERSION = "{version}"

_OTHER_ACTIONS = ["s1", "s2"]

_TOKEN_GATED_DESTRUCTIVE_ACTIONS = frozenset({{
    ("t0", "delete_track"),
}})


def _unknown(action, valid):
    return {{"error": "UNKNOWN_ACTION"}}


def _annotations_for_tool_name(tool_name):
    external_tools = ("layout_presets", "media_pool", "folder")
    destructive_tools = ("t0", "timeline", "timeline_item")
    if tool_name == "media_analysis":
        return "external_write"
    if tool_name in external_tools:
        return "external_destructive"
    if tool_name in destructive_tools:
        return "destructive"
    return "write"


@mcp.tool()
def t0(action: str, params=None):
    if action == "direct0":
        return {{"ok": True}}
    if action == "direct1":
        return {{"ok": True}}
    return _unknown(action, ["direct0", "direct1"])


@mcp.tool()
def t1(action: str, params=None):
    return _unknown(action, ["a", "b", "b", *_OTHER_ACTIONS])


{extra_tools}
"""

EXTRA_TOOL = """\

@mcp.tool()
def {name}(action: str, params=None):
    return _unknown(action, {actions})
"""

DISPATCH_ONLY_TOOL = """\

@mcp.tool()
def {name}(action: str, params=None):
    if action == "begin_run":
        return {{"ok": True}}
    if action == "end_run":
        return {{"ok": True}}
    return {{"error": "UNKNOWN"}}
"""

PIN_TEMPLATE = """\
{{
  "schema_version": "mcp-pin-v1",
  "source_url": "https://github.com/samuelgursky/davinci-resolve-mcp",
  "commit": "{commit}",
  "server_mode": "{server_mode}",
  "server_entry_point": "src/server.py",
  "server_protocol": "stdio",
  "venv_python": "{venv}",
  "venv_python_version": "3.12.10",
  "repo_python": "3.12.10",
  "advanced_server": {{"package": "davinci-resolve-advanced-mcp", "enabled": {advanced}}},
  "optional_deps": {{"ffmpeg": "required"}},
  "resolve_preference": "Local",
  "update_check": {{"enabled": false, "mechanism": "test fixture"}},
  "pinned_date": "2026-08-22T00:45:25Z"
}}
"""

KERNEL_README_TEMPLATE = """\
# Kernel Action Coverage

Current kernel coverage: **{stated} actions** across **9 compound MCP tools**.

| Kernel | MCP Tool | Actions |
|--------|----------|---------|
| Edit | `t1` | `a`, `b`, `missing_action` |
| Audio | `ghost_tool` | `a` |
"""


def _write_vendor_tree(
    tmp_path: Path,
    *,
    compound_tools: int = EXPECTED_COMPOUND_TOOLS,
    granular_tools: int = EXPECTED_GRANULAR_TOOLS,
    provider_version: str = "2.207.0",
    kernel_readme: str | None = None,
) -> Path:
    clone = tmp_path / "clone"
    (clone / "src" / "granular").mkdir(parents=True, exist_ok=True)
    (clone / "docs" / "kernels").mkdir(parents=True, exist_ok=True)
    (clone / "bin").mkdir(exist_ok=True)
    (clone / "bin" / "davinci-resolve-advanced-mcp.mjs").write_text("// stub\n")
    parts = [SERVER_TEMPLATE.format(version=provider_version, extra_tools="")]
    # t0 (dispatch-listed), t1 (duplicate + starred spread) already in template.
    for index in range(2, compound_tools):
        if index == compound_tools - 1:
            parts.append(DISPATCH_ONLY_TOOL.format(name=f"t{index}"))
        else:
            parts.append(EXTRA_TOOL.format(name=f"t{index}", actions='["x", "y"]'))
    (clone / "src" / "server.py").write_text("".join(parts))
    remaining = granular_tools - 2
    (clone / "src" / "granular" / "t1.py").write_text(
        "from src.granular.common import mcp\n\n"
        "@mcp.tool()\ndef a() -> str:\n    return 'a'\n\n"
        "@mcp.tool()\ndef t1_b() -> str:\n    return 'b'\n"
    )
    (clone / "src" / "granular" / "t2.py").write_text(
        "from src.granular.common import mcp\n\n"
        + "\n\n".join(
            f"@mcp.tool()\ndef g{index}() -> str:\n    return '{index}'"
            for index in range(remaining)
        )
        + "\n"
    )
    (clone / "src" / "granular" / "common.py").write_text("mcp = None\n")
    (clone / "docs" / "kernels" / "README.md").write_text(
        kernel_readme
        if kernel_readme is not None
        else KERNEL_README_TEMPLATE.format(stated=EXPECTED_KERNEL_ACTIONS)
    )
    return clone


def _surface(tmp_path: Path, **kwargs: Any) -> Any:
    return parse_vendor_surface(_write_vendor_tree(tmp_path, **kwargs))


def _live_tools(
    count: int = EXPECTED_COMPOUND_TOOLS, *, schema_seed: str = "v1"
) -> tuple[LiveTool, ...]:
    return tuple(
        LiveTool(
            name=f"t{index}",
            input_schema={
                "properties": {
                    "action": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["action"],
                "seed": schema_seed,
            },
        )
        for index in range(count)
    )


def _pin_facts() -> PinFacts:
    return PinFacts(
        commit=COMMIT,
        provider_version="2.207.0",
        server_mode="compound",
        advanced_enabled=True,
        handshake_name="DaVinciResolveMCP",
        handshake_version="1.29.1",
    )


def _build(tmp_path: Path, live: tuple[LiveTool, ...] | None = None, **kwargs: Any) -> Any:
    surface = _surface(tmp_path, **kwargs)
    return build_inventory(_pin_facts(), surface, live if live is not None else _live_tools())


def test_vendor_surface_parses_fixture_completely(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    assert len(surface.compound_tools) == EXPECTED_COMPOUND_TOOLS
    assert surface.provider_version == "2.207.0"
    by_name = {tool.name: tool for tool in surface.compound_tools}
    assert by_name["t0"].actions == ("direct0", "direct1")
    assert by_name["t1"].actions == ("a", "b", "s1", "s2")
    assert surface.duplicate_actions == (("t1", "b"),)
    assert surface.confirm_token_actions == (("t0", "delete_track"),)
    safety = {tool.name: tool.safety for tool in surface.compound_tools}
    assert safety["t0"] == "destructive"
    assert safety["t1"] == "write"
    assert safety["t2"] == "write"
    last = by_name[f"t{EXPECTED_COMPOUND_TOOLS - 1}"]
    assert last.actions == ("begin_run", "end_run")
    granular = dict(surface.granular_modules)
    assert granular["t1"] == ("a", "t1_b")
    assert sum(len(names) for names in granular.values()) == EXPECTED_GRANULAR_TOOLS


def test_build_inventory_passes_at_expected_counts_and_records_aliases(tmp_path: Path) -> None:
    inventory = _build(tmp_path)
    assert inventory.counts.compound_tools == EXPECTED_COMPOUND_TOOLS
    assert inventory.counts.granular_tools == EXPECTED_GRANULAR_TOOLS
    assert inventory.counts.kernel_actions == EXPECTED_KERNEL_ACTIONS
    operations = {row.operation_id: row for row in inventory.operations}
    assert operations["t1.a"].granular_aliases == ("a",)
    assert operations["t1.b"].granular_aliases == ("t1_b",)
    assert operations["t1.s1"].granular_aliases == ()
    assert operations["t0.direct0"].source == "compound"
    granular_only = set(inventory.cross_check.granular_only)
    assert "t1_b" not in granular_only
    assert len(granular_only) == EXPECTED_GRANULAR_TOOLS - 2
    assert inventory.cross_check.kernel_action_orphans == ("t1.missing_action",)
    assert inventory.cross_check.kernel_tool_orphans == ("ghost_tool",)
    assert inventory.cross_check.intra_tool_duplicate_actions == ("t1.b",)
    assert inventory.cross_check.compound_action_count > 0
    assert inventory.cross_check.operation_count == (
        inventory.cross_check.compound_action_count + len(granular_only)
    )
    kernel_rows = {(row.tool, row.action) for row in _surface(tmp_path).kernel_catalog.rows}
    assert ("t1", "a") in kernel_rows
    assert operations["t1.a"].kernel is True


def test_added_tool_fails_as_typed_drift(tmp_path: Path) -> None:
    with pytest.raises(McpCoverageDriftError) as excinfo:
        _build(tmp_path, live=_live_tools(EXPECTED_COMPOUND_TOOLS + 1))
    assert excinfo.value.code == "compound-tool-count-drift"


def test_removed_tool_fails_as_typed_drift(tmp_path: Path) -> None:
    with pytest.raises(McpCoverageDriftError) as excinfo:
        _build(tmp_path, live=_live_tools(EXPECTED_COMPOUND_TOOLS - 1))
    assert excinfo.value.code == "compound-tool-count-drift"


def test_live_name_outside_static_surface_fails_as_typed_drift(tmp_path: Path) -> None:
    mutated = list(_live_tools())
    mutated[3] = LiveTool(name="ghost", input_schema={"type": "object"})
    with pytest.raises(McpCoverageDriftError) as excinfo:
        _build(tmp_path, live=tuple(mutated))
    assert excinfo.value.code == "compound-tool-set-drift"


def test_schema_changed_tool_changes_schema_hash_and_inventory_digest(tmp_path: Path) -> None:
    before = _build(tmp_path)
    changed = _build(tmp_path, live=_live_tools(schema_seed="v2"))
    before_row = {row.name: row for row in before.compound_tools}["t0"]
    changed_row = {row.name: row for row in changed.compound_tools}["t0"]
    assert before_row.input_schema_sha256 != changed_row.input_schema_sha256
    assert schema_sha256({"type": "object"}) == schema_sha256({"type": "object"})
    assert inventory_bytes(before) != inventory_bytes(changed)


def test_granular_count_drift_fails_as_typed_drift(tmp_path: Path) -> None:
    with pytest.raises(McpCoverageDriftError) as excinfo:
        _build(tmp_path, granular_tools=EXPECTED_GRANULAR_TOOLS - 1)
    assert excinfo.value.code == "granular-tool-count-drift"


def test_kernel_count_drift_fails_as_typed_drift(tmp_path: Path) -> None:
    drifted = KERNEL_README_TEMPLATE.format(stated=EXPECTED_KERNEL_ACTIONS + 1)
    with pytest.raises(McpCoverageDriftError) as excinfo:
        _build(tmp_path, kernel_readme=drifted)
    assert excinfo.value.code == "kernel-action-count-drift"


def test_malformed_kernel_catalog_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(VendorSurfaceError) as no_statement:
        _surface(
            tmp_path,
            kernel_readme="# Kernel Action Coverage\n\nNo statement here.\n",
        )
    assert no_statement.value.code == "kernel-catalog-unparsable"
    with pytest.raises(VendorSurfaceError) as empty_table:
        _surface(
            tmp_path,
            kernel_readme=(
                "Current kernel coverage: **136 actions** across **9 compound MCP tools**.\n"
                "\nNo table rows at all.\n"
            ),
        )
    assert empty_table.value.code == "kernel-catalog-empty"


def test_missing_server_or_granular_source_fails_loud(tmp_path: Path) -> None:
    no_server = _write_vendor_tree(tmp_path / "a")
    (no_server / "src" / "server.py").unlink()
    with pytest.raises(VendorSurfaceError) as unreadable:
        parse_vendor_surface(no_server)
    assert unreadable.value.code == "server-unreadable"
    empty_module = _write_vendor_tree(tmp_path / "b")
    (empty_module / "src" / "granular" / "t1.py").write_text("mcp = None\n")
    with pytest.raises(VendorSurfaceError) as empty:
        parse_vendor_surface(empty_module)
    assert empty.value.code == "granular-module-empty"


def test_deterministic_output_two_builds_byte_identical(tmp_path: Path) -> None:
    first = _build(tmp_path)
    second = _build(tmp_path)
    assert inventory_bytes(first) == inventory_bytes(second)
    reversed_live = tuple(reversed(_live_tools()))
    third = build_inventory(_pin_facts(), _surface(tmp_path), reversed_live)
    assert inventory_bytes(third) == inventory_bytes(first)


def test_write_inventory_tree_writes_inventory_and_manifest(tmp_path: Path) -> None:
    inventory = _build(tmp_path)
    out = tmp_path / "coverage"
    write_inventory_tree(out, inventory_bytes(inventory))
    assert (out / "inventory.json").read_bytes() == inventory_bytes(inventory)
    manifest = build_manifest(out)
    assert [entry.path for entry in manifest.entries] == ["inventory.json"]
    assert (out / MANIFEST_NAME).is_file()
    digest = json.loads((out / "inventory.json").read_text(encoding="utf-8"))["cross_check"]
    assert digest["static_tool_names_match_live"] is True


def _init_git_clone(clone: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=clone, check=True)


def _write_pin(tmp_path: Path, **overrides: Any) -> Path:
    values: dict[str, Any] = {
        "commit": COMMIT,
        "server_mode": "compound",
        "venv": str(tmp_path / "venv" / "bin" / "python"),
        "advanced": "true",
    }
    values.update(overrides)
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(
        PIN_TEMPLATE.format(
            commit=values["commit"],
            server_mode=values["server_mode"],
            venv=values["venv"],
            advanced=values["advanced"],
        ),
        encoding="utf-8",
    )
    return pin_path


def test_wrong_checkout_head_fails_typed(tmp_path: Path) -> None:
    clone = _write_vendor_tree(tmp_path)
    _init_git_clone(clone)
    with pytest.raises(InventoryCommandError) as excinfo:
        generate_inventory(_write_pin(tmp_path), clone)
    assert excinfo.value.code == "checkout-head-mismatch"


def test_unreadable_head_fails_typed(tmp_path: Path) -> None:
    with pytest.raises(InventoryCommandError) as excinfo:
        _check_head(tmp_path / "not-a-repo", COMMIT)
    assert excinfo.value.code == "checkout-head-unreadable"


def test_provider_version_mismatch_fails_typed_before_spawn(tmp_path: Path) -> None:
    clone = _write_vendor_tree(tmp_path, provider_version="9.9.9")
    _init_git_clone(clone)
    head = _git_head(clone)
    with pytest.raises(InventoryCommandError) as excinfo:
        generate_inventory(_write_pin(tmp_path, commit=head), clone)
    assert excinfo.value.code == "provider-version-mismatch"


def _git_head(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_non_compound_pin_rejected_at_pin_parse(tmp_path: Path) -> None:
    with pytest.raises(McpPinError):
        load_mcp_pin(_write_pin(tmp_path, server_mode="granular"))


def test_advanced_enabled_but_missing_fails_typed(tmp_path: Path) -> None:
    clone = _write_vendor_tree(tmp_path)
    (clone / "bin" / "davinci-resolve-advanced-mcp.mjs").unlink()
    with pytest.raises(InventoryCommandError) as excinfo:
        _check_advanced(clone, advanced_enabled=True)
    assert excinfo.value.code == "advanced-missing"


def test_malformed_live_schema_rejected_at_boundary() -> None:
    with pytest.raises(ValidationError):
        LiveTool(name="t0", input_schema=["not", "a", "schema", "object"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LiveTool(name="", input_schema={"type": "object"})


def test_generation_code_issues_no_tool_calls() -> None:
    """Inventory generation must be read-only: no tool-call seam is reachable."""
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in [
            root / "services" / "cli" / "mcp_inventory.py",
            root / "services" / "mcp_client" / "discovery.py",
            root / "services" / "toolchain" / "mcp_coverage.py",
            root / "services" / "toolchain" / "mcp_coverage_models.py",
            root / "services" / "toolchain" / "mcp_vendor_surface.py",
            root / "services" / "toolchain" / "mcp_vendor_server.py",
        ]
    }
    for name, text in sources.items():
        assert "_call_tool" not in text, name
        assert "_call_action_json" not in text, name
        assert "tools/call" not in text, name
    cli = sources["mcp_inventory.py"]
    assert "McpClient" not in cli
    assert "list_tools()" in cli
    assert "discovery.close()" in cli


def test_committed_inventory_exists_and_matches_drift_guards() -> None:
    """The committed real inventory carries the pinned 35/353/136 counts."""
    inventory_path = (
        Path(__file__).resolve().parents[2] / "capabilities" / "mcp-coverage" / "inventory.json"
    )
    if not inventory_path.is_file():  # pragma: no cover - generation ran first
        pytest.skip("inventory not generated yet")
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "mcp-inventory-v1"
    assert payload["counts"] == {
        "compound_tools": EXPECTED_COMPOUND_TOOLS,
        "granular_tools": EXPECTED_GRANULAR_TOOLS,
        "kernel_actions": EXPECTED_KERNEL_ACTIONS,
    }
    assert payload["pin"]["commit"] == COMMIT
    assert payload["pin"]["provider_version"] == "3.2.0"
    assert payload["pin"]["server_mode"] == "compound"





# ─── Task 10 — reviewed dispositions and the zero-gap validator ──────────────

COVERAGE_DIR = Path(__file__).resolve().parents[2] / "capabilities" / "mcp-coverage"
REAL_INVENTORY = COVERAGE_DIR / "inventory.json"
REAL_DISPOSITIONS = COVERAGE_DIR / "dispositions.json"
REAL_MAPPED = 73
REAL_ALIASES = 248
MINI_EVIDENCE = "tests/mini-evidence.md"


def _op(
    tool: str, action: str, *, source: str, safety: str, token: bool = False
) -> OperationRow:
    return OperationRow.model_validate({
        "operation_id": f"{tool}.{action}",
        "tool": tool,
        "action": action,
        "source": source,
        "compound_aliases": (f"{tool}.{action}",) if source == "compound" else (),
        "granular_aliases": (action,) if source == "granular_only" else (),
        "safety": safety,
        "requires_confirm_token": token,
        "kernel": False,
    })


def _mini_inventory(*extras: OperationRow) -> McpInventoryV1:
    real = [
        _op("timeline", "get_current", source="compound", safety="destructive"),
        _op("media_pool", "safe_import_media", source="compound", safety="external_destructive"),
        _op("media_pool", "create_timeline", source="compound", safety="external_destructive"),
        _op("project_manager", "get_current", source="compound", safety="destructive"),
        _op("timeline", "create_timeline", source="granular_only", safety="destructive"),
    ]
    fictional = [
        _op("t_a", "get_settings", source="compound", safety="write"),
        _op("t_a", "set_thing", source="compound", safety="write"),
        _op("t_b", "delete_all", source="compound", safety="external_destructive", token=True),
        _op("t_b", "get_list", source="compound", safety="external_destructive"),
        _op("g", "get_only", source="granular_only", safety="unmapped"),
    ]
    operations = [*real, *fictional, *extras]
    tools = (
        CompoundToolRow(name="t_a", input_schema_sha256="a" * 64, safety="write",
                        actions=("get_settings", "set_thing")),
        CompoundToolRow(name="t_b", input_schema_sha256="b" * 64, safety="external_destructive",
                        actions=("delete_all", "get_list")),
        CompoundToolRow(name="timeline", input_schema_sha256="c" * 64, safety="destructive",
                        actions=("get_current", "create_timeline")),
        CompoundToolRow(name="media_pool", input_schema_sha256="d" * 64,
                        safety="external_destructive",
                        actions=("safe_import_media", "create_timeline")),
        CompoundToolRow(name="project_manager", input_schema_sha256="e" * 64,
                        safety="destructive", actions=("get_current",)),
    )
    return McpInventoryV1(
        schema_version=INVENTORY_SCHEMA,
        pin=PinFacts(
            commit=COMMIT,
            provider_version="2.207.0",
            server_mode="compound",
            advanced_enabled=True,
            handshake_name="DaVinciResolveMCP",
            handshake_version="1.29.1",
        ),
        counts=InventoryCounts(compound_tools=5, granular_tools=2, kernel_actions=0),
        compound_tools=tools,
        granular_tools=(
            GranularToolRow(module="g", name="get_only"),
            GranularToolRow(module="timeline", name="create_timeline"),
        ),
        operations=tuple(operations),
        kernel_catalog_stated_actions=0,
        kernel_catalog_stated_tools=0,
        kernel_catalog_rows=0,
        cross_check=CrossCheckFacts(
            static_tool_names_match_live=True,
            compound_action_count=6,
            operation_count=len(operations),
            granular_alias_count=0,
            granular_only=("get_only", "create_timeline"),
            kernel_action_orphans=(),
            kernel_tool_orphans=(),
            intra_tool_duplicate_actions=(),
        ),
    )


def _surface_route(target: str) -> dict[str, str]:
    return {"kind": "surface", "target": target}


def _ops_read_route(target: str) -> dict[str, str]:
    return {"kind": "ops_read", "target": target}


def _planned_route(target: str) -> dict[str, str]:
    return {"kind": "planned", "target": target}


def _row(  # noqa: PLR0913 (row-builder carries the full disposition shape)
    operation_id: str,
    category: str,
    status: str,
    route: dict[str, str],
    *,
    alias_of: str | None = None,
    owner: str = "",
    reason: str = "",
    target_phase: str = "",
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    optional = {
        field: value
        for field, value in (
            ("owner", owner), ("reason", reason), ("target_phase", target_phase),
        )
        if value
    }
    entry: dict[str, Any] = {
        "operation_id": operation_id,
        "category": category,
        "status": status,
        "route": route,
        **optional,
    }
    if alias_of is not None:
        entry["alias_of"] = alias_of
    entry["evidence"] = evidence if evidence is not None else ["vendor:src/server.py:x"]
    return entry


def _mapped(
    operation_id: str,
    category: str,
    route: dict[str, str],
    evidence: list[str],
    *,
    alias_of: str | None = None,
) -> dict[str, Any]:
    return _row(operation_id, category, "mapped", route, alias_of=alias_of, evidence=evidence)


def _deferred(
    operation_id: str,
    category: str,
    target: str = "task13:read-session",
    *,
    alias_of: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return _row(
        operation_id,
        category,
        "deferred",
        _planned_route(target),
        alias_of=alias_of,
        owner="owner",
        reason="reason",
        target_phase="phase",
        evidence=evidence,
    )


def _mini_rows(ev: str) -> list[dict[str, Any]]:
    return [
        _mapped("timeline.get_current", "read", _surface_route("prepare_project"), [ev]),
        _mapped("media_pool.safe_import_media", "guarded_mutation",
                _surface_route("safe_import_media"), [ev]),
        _mapped("media_pool.create_timeline", "guarded_mutation",
                _surface_route("prepare_project"), [ev]),
        _mapped("timeline.create_timeline", "guarded_mutation",
                _surface_route("prepare_project"),
                ["vendor:src/granular/timeline.py:create_timeline", ev],
                alias_of="media_pool.create_timeline"),
        _mapped("project_manager.get_current", "read",
                _ops_read_route("get_current_project"), ["vendor:src/server.py:x", ev]),
        _deferred("t_a.get_settings", "read"),
        _deferred("t_a.set_thing", "mutate", "task14:plan-step-surface"),
        _deferred("t_b.delete_all", "guarded_mutation", "task15:guarded-handler",
                  evidence=["vendor:src/server.py:t_b.delete_all"]),
        _deferred("t_b.get_list", "read", evidence=["vendor:src/server.py:t_b.get_list"]),
        _deferred("g.get_only", "read", evidence=["vendor:src/granular/g.py:get_only"]),
    ]


def _write_pair(
    tmp_path: Path, inventory: McpInventoryV1, rows: list[dict[str, Any]], *, pin: str = COMMIT
) -> tuple[Path, Path]:
    inv_path = tmp_path / "inventory.json"
    payload = json.loads(inventory.model_dump_json())
    inv_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), "utf-8")
    digest = hashlib.sha256(inv_path.read_bytes()).hexdigest()
    disp_path = tmp_path / "dispositions.json"
    disp_path.write_text(
        json.dumps(
            {
                "schema_version": "mcp-dispositions-v1",
                "pin_commit": pin,
                "inventory_sha256": digest,
                "rows": rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "utf-8",
    )
    return inv_path, disp_path


def _mini_pair(
    tmp_path: Path,
    rows: list[dict[str, Any]] | None = None,
    *,
    extras: tuple[OperationRow, ...] = (),
    pin: str = COMMIT,
) -> tuple[Path, Path]:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "mini-evidence.md").write_text("ev\n", "utf-8")
    return _write_pair(
        tmp_path,
        _mini_inventory(*extras),
        rows if rows is not None else _mini_rows(MINI_EVIDENCE),
        pin=pin,
    )


def test_mini_fixture_validates_and_reports_domain_gaps(tmp_path: Path) -> None:
    inv, disp = _mini_pair(tmp_path)
    report = validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert report.total_operations == 10
    assert (report.mapped, report.deferred) == (5, 5)
    assert report.deferred_by_category == {
        "read": 3, "mutate": 1, "guarded_mutation": 1, "session_control": 0,
    }
    assert report.deferred_by_domain == {"t_a": 2, "t_b": 2, "g": 1}
    assert report.unmapped == 0
    assert report.refused_vendor_supported == 0


def test_new_inventory_operation_without_disposition_row_fails(tmp_path: Path) -> None:
    inv, disp = _mini_pair(
        tmp_path, extras=(_op("t_a", "brand_new", source="compound", safety="write"),)
    )
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-missing-rows"
    assert "t_a.brand_new" in excinfo.value.detail


def test_duplicate_disposition_row_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows.append(_mapped("media_pool.create_timeline", "guarded_mutation",
                        _surface_route("prepare_project"), [MINI_EVIDENCE]))
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-duplicate-operation"


def test_stale_inventory_hash_pin_fails(tmp_path: Path) -> None:
    inv, disp = _mini_pair(tmp_path)
    payload = json.loads(disp.read_text("utf-8"))
    payload["inventory_sha256"] = "0" * 64
    disp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), "utf-8")
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-inventory-hash-mismatch"


def test_stale_pin_commit_fails(tmp_path: Path) -> None:
    inv, disp = _mini_pair(tmp_path, pin=OTHER_SHA)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-pin-mismatch"


def test_absent_evidence_path_fails(tmp_path: Path) -> None:
    rows = _mini_rows("tests/no-such-file.md")
    rows[1:] = _mini_rows(MINI_EVIDENCE)[1:]
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-evidence-path-missing"


def test_mapped_row_without_repo_evidence_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[0] = _mapped("timeline.get_current", "read",
                      _surface_route("prepare_project"), ["vendor:src/server.py:x"])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-evidence-missing"


def test_deferred_row_with_missing_owner_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[7] = _row(
        "t_b.delete_all",
        "guarded_mutation",
        "deferred",
        _planned_route("task15:guarded-handler"),
        owner="",
        reason="r",
        target_phase="p",
    )
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-deferred-metadata-missing"


def test_refused_status_and_unknown_category_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        DispositionRow.model_validate(
            _row("t_a.set_thing", "mutate", "refused", _planned_route("task15:guarded-handler"))
        )
    with pytest.raises(ValidationError):
        DispositionRow.model_validate(
            _row("t_a.set_thing", "ignored", "deferred", _planned_route("task15:guarded-handler"))
        )


def test_confirm_token_row_downgraded_to_mutate_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[7] = _deferred("t_b.delete_all", "mutate", "task14:plan-step-surface")
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-risk-downgrade"


def test_destructive_row_downgraded_to_mutate_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[8] = _deferred("t_b.get_list", "mutate", "task14:plan-step-surface")
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-risk-downgrade"


def test_destructive_row_relabelled_read_without_read_name_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[1] = _mapped("media_pool.safe_import_media", "read",
                      _surface_route("safe_import_media"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-read-unproven"


def test_mapped_surface_must_be_exactly_bound_not_any_supported_surface(
    tmp_path: Path,
) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[1] = _mapped("media_pool.safe_import_media", "guarded_mutation",
                      _surface_route("prepare_project"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-surface-not-bound"


def test_mapped_ops_read_method_must_be_exactly_bound(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[4] = _mapped("project_manager.get_current", "read",
                      _ops_read_route("detect_gaps_overlaps"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-bound"


def test_fictional_operation_cannot_borrow_any_supported_surface(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[6] = _mapped("t_a.set_thing", "guarded_mutation",
                      _surface_route("prepare_project"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-surface-not-bound"
    rows[6] = _mapped("t_a.set_thing", "guarded_mutation",
                      _surface_route("safe_import_media"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-surface-not-bound"


def test_fictional_operation_cannot_borrow_any_ops_read_method(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[5] = _mapped("t_a.get_settings", "read",
                      _ops_read_route("get_current_project"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-bound"


def test_mapped_unsupported_surface_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[1] = _mapped("media_pool.safe_import_media", "guarded_mutation",
                      _surface_route("timeline.purge_everything"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-surface-not-supported"


def test_mapped_nonexistent_or_unreviewed_ops_read_method_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[4] = _mapped("project_manager.get_current", "read",
                      _ops_read_route("no_such_method"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-reviewed"
    rows[4] = _mapped("project_manager.get_current", "read",
                      _ops_read_route("render_start"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-reviewed"
    assert "render_start" in excinfo.value.detail


def test_ops_read_route_on_mutation_row_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[7] = _mapped("t_b.delete_all", "guarded_mutation",
                      _ops_read_route("get_current_project"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-read"


def test_alias_route_must_match_target_route_exactly(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[3] = _mapped("timeline.create_timeline", "guarded_mutation",
                      _surface_route("render_native"),
                      ["vendor:src/granular/timeline.py:create_timeline", MINI_EVIDENCE],
                      alias_of="media_pool.create_timeline")
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-alias-inheritance-mismatch"


def test_alias_of_unbound_compound_target_cannot_borrow_a_route(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows.append(_mapped("t_c.never_bound", "guarded_mutation",
                        _surface_route("prepare_project"), [MINI_EVIDENCE]))
    inv, disp = _mini_pair(
        tmp_path, rows, extras=(_op("t_c", "never_bound", source="compound", safety="write"),)
    )
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-surface-not-bound"


def test_alias_chain_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    chain_row = _deferred(
        "g.get_only",
        "read",
        alias_of="timeline.create_timeline",
        evidence=["vendor:src/granular/g.py:get_only", "vendor:src/server.py:timeline"],
    )
    inv, disp = _mini_pair(tmp_path, [*rows[:9], chain_row])
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-alias-chain"


def test_alias_target_must_be_compound_row(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[3] = _deferred(
        "timeline.create_timeline",
        "read",
        alias_of="g.get_only",
        evidence=["vendor:src/granular/timeline.py:create_timeline",
                  "vendor:src/granular/g.py:get_only"],
    )
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-alias-target-invalid"


def test_alias_source_must_be_granular_only(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[8] = _deferred(
        "t_b.get_list",
        "read",
        alias_of="timeline.get_current",
        evidence=["vendor:src/server.py:t_b.get_list", "vendor:src/server.py:timeline"],
    )
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-alias-source-invalid"


def test_granular_only_row_without_alias_cannot_map(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[9] = _mapped("g.get_only", "read", _ops_read_route("get_transform"), [MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-ops-read-not-bound"


def test_session_control_outside_review_list_fails(tmp_path: Path) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    rows[5] = _deferred("t_a.get_settings", "session_control", evidence=[MINI_EVIDENCE])
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsError) as excinfo:
        validate_dispositions_files(inv, disp, repo_root=tmp_path)
    assert excinfo.value.code == "dispositions-session-control-unreviewed"


def test_binding_authority_covers_every_supported_surface_and_reviewed_method() -> None:
    assert frozenset(SURFACE_OPERATION_BINDINGS) == frozenset(SUPPORTED_SURFACES)
    assert set(OPS_READ_OPERATION_BINDINGS.values()) == set(OPS_READ_METHODS)


def test_committed_route_bindings_verify_against_live_sources() -> None:
    verify_route_bindings(committed_handler_sources(), committed_ops_source())


def test_stale_surface_binding_fails_static_verification() -> None:
    sources = committed_handler_sources()
    doctored = {
        name: text.replace(
            '"project_manager", "create"', '"project_manager", "renamed_create"'
        )
        for name, text in sources.items()
    }
    assert any("renamed_create" in text for text in doctored.values())
    with pytest.raises(DispositionsError) as excinfo:
        verify_route_bindings(doctored, committed_ops_source())
    assert excinfo.value.code == "bindings-surface-stale"
    assert "project_manager.create" in excinfo.value.detail


def test_stale_ops_read_binding_fails_static_verification() -> None:
    ops = committed_ops_source()
    renamed = ops.replace('"project_manager", "get_current"',
                          '"project_manager", "renamed_get_current"')
    with pytest.raises(DispositionsError) as excinfo:
        verify_route_bindings(committed_handler_sources(), renamed)
    assert excinfo.value.code == "bindings-ops-stale"
    removed = ops.replace("def get_current_project", "def renamed_get_current_project")
    with pytest.raises(DispositionsError) as excinfo:
        verify_route_bindings(committed_handler_sources(), removed)
    assert excinfo.value.code == "bindings-ops-method-missing"


def _strict_inventory() -> McpInventoryV1:
    operations = (
        _op("timeline", "get_current", source="compound", safety="destructive"),
        _op("media_pool", "safe_import_media", source="compound",
            safety="external_destructive"),
        _op("media_pool", "create_timeline", source="compound",
            safety="external_destructive"),
        _op("project_manager", "get_current", source="compound", safety="destructive"),
        _op("timeline", "create_timeline", source="granular_only", safety="destructive"),
        _op("timeline_item", "get_transform", source="compound", safety="destructive"),
        _op("render", "delete_job", source="compound", safety="external_destructive"),
        _op("timeline", "voice_isolation_capabilities", source="compound",
            safety="destructive"),
    )
    return McpInventoryV1(
        schema_version=INVENTORY_SCHEMA,
        pin=_pin_facts(),
        counts=InventoryCounts(compound_tools=5, granular_tools=1, kernel_actions=0),
        compound_tools=(
            CompoundToolRow(name="timeline", input_schema_sha256="c" * 64,
                            safety="destructive",
                            actions=("get_current", "voice_isolation_capabilities")),
            CompoundToolRow(name="media_pool", input_schema_sha256="d" * 64,
                            safety="external_destructive",
                            actions=("safe_import_media", "create_timeline")),
            CompoundToolRow(name="project_manager", input_schema_sha256="e" * 64,
                            safety="destructive", actions=("get_current",)),
            CompoundToolRow(name="timeline_item", input_schema_sha256="f" * 64,
                            safety="destructive", actions=("get_transform",)),
            CompoundToolRow(name="render", input_schema_sha256="1" * 64,
                            safety="external_destructive", actions=("delete_job",)),
        ),
        granular_tools=(GranularToolRow(module="timeline", name="create_timeline"),),
        operations=operations,
        kernel_catalog_stated_actions=0,
        kernel_catalog_stated_tools=0,
        kernel_catalog_rows=0,
        cross_check=CrossCheckFacts(
            static_tool_names_match_live=True,
            compound_action_count=7,
            operation_count=len(operations),
            granular_alias_count=0,
            granular_only=("create_timeline",),
            kernel_action_orphans=(),
            kernel_tool_orphans=(),
            intra_tool_duplicate_actions=(),
        ),
    )


def test_strict_mode_passes_only_at_full_mapping(tmp_path: Path) -> None:
    mapped = [
        _mapped("timeline.get_current", "read", _surface_route("prepare_project"),
                [MINI_EVIDENCE]),
        _mapped("media_pool.safe_import_media", "guarded_mutation",
                _surface_route("safe_import_media"), [MINI_EVIDENCE]),
        _mapped("media_pool.create_timeline", "guarded_mutation",
                _surface_route("prepare_project"), [MINI_EVIDENCE]),
        _mapped("timeline.create_timeline", "guarded_mutation",
                _surface_route("prepare_project"),
                ["vendor:src/granular/timeline.py:create_timeline", MINI_EVIDENCE],
                alias_of="media_pool.create_timeline"),
        _mapped("project_manager.get_current", "read",
                _ops_read_route("get_current_project"), [MINI_EVIDENCE]),
        _mapped("timeline_item.get_transform", "read",
                _ops_read_route("get_transform"),
                ["vendor:src/server.py:timeline_item.get_transform", MINI_EVIDENCE]),
        _mapped("render.delete_job", "guarded_mutation", _surface_route("render_native"),
                ["vendor:src/server.py:render.delete_job", MINI_EVIDENCE]),
        _mapped("timeline.voice_isolation_capabilities", "read",
                _ops_read_route("voice_isolation_capabilities"), [MINI_EVIDENCE]),
    ]
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "mini-evidence.md").write_text("ev\n", "utf-8")
    inv, disp = _write_pair(tmp_path, _strict_inventory(), mapped)
    report = validate_dispositions_files(inv, disp, strict=True, repo_root=tmp_path)
    assert report.mode == "final"
    assert report.deferred == 0
    assert report.coverage == 1.0


def test_strict_mode_fails_when_any_real_deferred_row_remains_in_fixture(
    tmp_path: Path,
) -> None:
    rows = _mini_rows(MINI_EVIDENCE)
    inv, disp = _mini_pair(tmp_path, rows)
    with pytest.raises(DispositionsParityError) as excinfo:
        validate_dispositions_files(inv, disp, strict=True, repo_root=tmp_path)
    assert excinfo.value.code == "parity-deferred-remaining"


def test_committed_dispositions_validate_in_rollout_mode() -> None:
    if not REAL_DISPOSITIONS.is_file():  # pragma: no cover - generation ran first
        pytest.skip("dispositions not generated yet")
    report = validate_dispositions_files(REAL_INVENTORY, REAL_DISPOSITIONS)
    assert report.total_operations == 1090
    assert report.mapped == REAL_MAPPED
    assert report.deferred == 1090 - REAL_MAPPED
    assert report.coverage == REAL_MAPPED / 1090
    assert report.unmapped == 0
    assert report.refused_vendor_supported == 0
    assert len(report.deferred_by_domain) == 39
    assert "project" in report.deferred_by_domain
    assert sum(report.deferred_by_domain.values()) == report.deferred
    assert set(report.deferred_by_category) == {
        "read", "mutate", "guarded_mutation", "session_control",
    }


def test_strict_mode_fails_today_because_deferred_rows_remain() -> None:
    if not REAL_DISPOSITIONS.is_file():  # pragma: no cover - generation ran first
        pytest.skip("dispositions not generated yet")
    with pytest.raises(DispositionsParityError) as excinfo:
        validate_dispositions_files(REAL_INVENTORY, REAL_DISPOSITIONS, strict=True)
    assert excinfo.value.code == "parity-deferred-remaining"
    assert str(1090 - REAL_MAPPED) in excinfo.value.detail
    assert "timeline" in excinfo.value.detail


def test_committed_dispositions_resolve_reviewed_aliases() -> None:
    if not REAL_DISPOSITIONS.is_file():  # pragma: no cover - generation ran first
        pytest.skip("dispositions not generated yet")
    dispositions = load_dispositions(REAL_DISPOSITIONS)
    aliases = {row.operation_id: row.alias_of for row in dispositions.rows if row.alias_of}
    mapped_aliases = [
        row for row in dispositions.rows if row.alias_of and row.status == "mapped"
    ]
    assert len(aliases) == REAL_ALIASES
    assert len(mapped_aliases) == 22
    assert aliases["timeline.create_timeline"] == "media_pool.create_timeline"
    assert aliases["folder.export_folder"] == "folder.export"
    assert "media_pool_item.get_clip_unique_id_by_name" not in aliases


def test_committed_dispositions_bytes_are_canonical() -> None:
    if not REAL_DISPOSITIONS.is_file():  # pragma: no cover - generation ran first
        pytest.skip("dispositions not generated yet")
    dispositions = load_dispositions(REAL_DISPOSITIONS)
    canonical = dispositions_canonical_bytes(dispositions)
    assert canonical + b"\n" == REAL_DISPOSITIONS.read_bytes()
    assert dispositions_canonical_bytes(load_dispositions(REAL_DISPOSITIONS)) == canonical


def test_committed_manifest_pins_inventory_and_dispositions_bytes() -> None:
    manifest_path = COVERAGE_DIR / MANIFEST_NAME
    if not manifest_path.is_file():  # pragma: no cover - generation ran first
        pytest.skip("manifest not generated yet")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    entries = {entry["path"]: entry["sha256"] for entry in manifest["entries"]}
    assert set(entries) == {"inventory.json", "dispositions.json"}
    for name, digest in entries.items():
        assert hashlib.sha256((COVERAGE_DIR / name).read_bytes()).hexdigest() == digest
