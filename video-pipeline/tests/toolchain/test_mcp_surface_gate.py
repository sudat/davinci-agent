"""Task 11 — fail-closed committed-surface gate contract.

Fixtures are fully synthetic (tmp_path, no network, no server spawn): a mini
vendor checkout, a committed coverage artifact triple, and a pin file. Every
failure is asserted by typed error code; drift reports are proven to carry
ONLY sorted IDs and safe identifiers — never raw schemas, secrets, or paths.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.mcp_client.errors import McpSurfaceConfigError, McpToolSurfaceDriftError
from services.release.manifest import build_manifest, manifest_bytes
from services.toolchain.mcp_coverage_models import LiveTool
from services.toolchain.mcp_pin import McpPin, load_mcp_pin
from services.toolchain.mcp_surface_gate import (
    CommittedSurface,
    committed_surface_status,
    load_committed_surface,
)
from tests.toolchain.mcp_surface_support import (
    OTHER_SHA,
    git_commit_clone,
    schema_hashes_for,
    write_coverage_pair,
    write_mini_clone,
    write_pin,
)

MINI_TOOLS: dict[str, tuple[str, ...]] = {
    "resolve_control": ("get_version", "delete_everything"),
    "echo": ("back",),
}
MINI_LIVE: tuple[LiveTool, ...] = (
    LiveTool(
        name="resolve_control",
        input_schema={"type": "object", "properties": {"action": {"type": "string"}}},
    ),
    LiveTool(name="echo", input_schema={"type": "object"}),
)
SECRET_SCHEMA_MARKER = "akane-put-the-camera-raw-back"  # noqa: S105 (poison marker proving drift reports never leak payload text)
MEDIA_PATH_MARKER = "reference-episodes/v44-real-01"


@dataclass
class _Baseline:
    surface: CommittedSurface
    pin_path: Path
    pin: McpPin
    coverage_dir: Path
    clone_dir: Path

    def reload(self) -> CommittedSurface:
        return load_committed_surface(
            pin=self.pin, clone_dir=self.clone_dir, coverage_dir=self.coverage_dir
        )


def _live_schema_hashes() -> dict[str, str]:
    return schema_hashes_for(
        [{"name": tool.name, "inputSchema": dict(tool.input_schema)} for tool in MINI_LIVE]
    )


def _baseline(
    tmp_path: Path,
    *,
    pin_commit: str | None = None,
    clone_tools: dict[str, tuple[str, ...]] | None = None,
    schemas: dict[str, str] | None = None,
    version: str = "2.98.3",
) -> _Baseline:
    """One consistent baseline: clone + git HEAD + artifact triple + pin.

    ``pin_commit`` overrides the committed/pinned commit for every artifact
    at once (the wrong-checkout fixture: artifacts agree on a commit the
    checkout HEAD does not match).
    """
    clone = write_mini_clone(
        tmp_path / "tree",
        clone_tools if clone_tools is not None else MINI_TOOLS,
        version=version,
    )
    head = git_commit_clone(clone)
    committed = pin_commit if pin_commit is not None else head
    coverage, _ = write_coverage_pair(
        tmp_path / "tree",
        pin_commit=committed,
        schemas=schemas if schemas is not None else _live_schema_hashes(),
        actions=MINI_TOOLS,
    )
    pin = write_pin(tmp_path / "tree", commit=committed, venv_python="/venv/bin/python")
    surface = load_committed_surface(
        pin=load_mcp_pin(pin), clone_dir=clone, coverage_dir=coverage
    )
    return _Baseline(surface, pin, load_mcp_pin(pin), coverage, clone)


def test_exact_match_validates_static_and_installed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    baseline.surface.validate_static()
    baseline.surface.validate_installed(MINI_LIVE)


def test_missing_inventory_file_fails_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    (baseline.coverage_dir / "inventory.json").unlink()
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-inventory-missing"


def test_malformed_inventory_fails_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    (baseline.coverage_dir / "inventory.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-inventory-unparsable"


def test_missing_malformed_and_bad_seal_dispositions_fail_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    dispositions = baseline.coverage_dir / "dispositions.json"
    dispositions.unlink()
    with pytest.raises(McpSurfaceConfigError) as missing:
        baseline.reload()
    assert missing.value.code == "surface-dispositions-missing"
    dispositions.write_text("[1]", encoding="utf-8")
    with pytest.raises(McpSurfaceConfigError) as malformed:
        baseline.reload()
    assert malformed.value.code == "surface-dispositions-unparsable"
    dispositions.write_text(
        json.dumps({"pin_commit": "x" * 40, "inventory_sha256": 1}), encoding="utf-8"
    )
    with pytest.raises(McpSurfaceConfigError) as bad_seal:
        baseline.reload()
    assert bad_seal.value.code == "surface-dispositions-unparsable"


def test_stale_dispositions_inventory_hash_fails_typed(tmp_path: Path) -> None:
    """A pin upgrade that regenerated inventory + manifest but kept the old
    dispositions seal: bytes agree, the artifact pin does not."""
    baseline = _baseline(tmp_path)
    dispositions = baseline.coverage_dir / "dispositions.json"
    payload = json.loads(dispositions.read_text("utf-8"))
    payload["inventory_sha256"] = "0" * 64
    dispositions.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    manifest = baseline.coverage_dir / "manifest.json"
    manifest.write_bytes(manifest_bytes(build_manifest(baseline.coverage_dir)))
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-inventory-hash-stale"


def test_pin_commit_disagreement_fails_typed(tmp_path: Path) -> None:
    clone = write_mini_clone(tmp_path, MINI_TOOLS)
    head = git_commit_clone(clone)
    coverage, _ = write_coverage_pair(
        tmp_path, pin_commit=OTHER_SHA, schemas=_live_schema_hashes(), actions=MINI_TOOLS
    )
    pin = write_pin(tmp_path, commit=head, venv_python="/venv/bin/python")
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        load_committed_surface(
            pin=load_mcp_pin(pin), clone_dir=clone, coverage_dir=coverage
        )
    assert excinfo.value.code == "surface-pin-commit-mismatch"


def test_manifest_digest_mismatch_fails_typed(tmp_path: Path) -> None:
    """A pin upgrade that regenerates inventory bytes alone breaks the seal."""
    baseline = _baseline(tmp_path)
    payload = json.loads((baseline.coverage_dir / "inventory.json").read_text("utf-8"))
    payload["counts"]["compound_tools"] = 99
    (baseline.coverage_dir / "inventory.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-manifest-mismatch"


def test_manifest_entry_set_drift_fails_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    manifest_path = baseline.coverage_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["entries"].append({"path": "extra.json", "size": 1, "sha256": "0" * 64})
    manifest["entries"].sort(key=lambda entry: entry["path"])
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-manifest-entry-set"


def test_missing_manifest_fails_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    (baseline.coverage_dir / "manifest.json").unlink()
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        baseline.reload()
    assert excinfo.value.code == "surface-manifest-missing"


def test_invalid_pin_fails_typed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    pin = tmp_path / "pin.json"
    pin.write_text("{}", encoding="utf-8")
    verdict = committed_surface_status(
        pin_path=pin,
        clone_dir=baseline.clone_dir,
        coverage_dir=baseline.coverage_dir,
    )
    assert verdict.ok is False
    assert verdict.code == "surface-pin-invalid"


def test_wrong_checkout_head_is_drift_even_with_identical_content(tmp_path: Path) -> None:
    """Artifacts agree on a commit; the checkout HEAD does not match it."""
    baseline = _baseline(tmp_path, pin_commit=OTHER_SHA)
    head = subprocess.run(
        ["git", "-C", str(baseline.clone_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with pytest.raises(McpToolSurfaceDriftError) as excinfo:
        baseline.surface.validate_static()
    assert excinfo.value.findings["checkout-head"] == (head, OTHER_SHA)


def test_checkout_head_unreadable_fails_typed_config(tmp_path: Path) -> None:
    clone = write_mini_clone(tmp_path / "tree", MINI_TOOLS)  # never git-init'd
    coverage, _ = write_coverage_pair(
        tmp_path / "tree",
        pin_commit=OTHER_SHA,
        schemas=_live_schema_hashes(),
        actions=MINI_TOOLS,
    )
    pin = write_pin(tmp_path / "tree", commit=OTHER_SHA, venv_python="/venv/bin/python")
    surface = load_committed_surface(
        pin=load_mcp_pin(pin), clone_dir=clone, coverage_dir=coverage
    )
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        surface.validate_static()
    assert excinfo.value.code == "surface-checkout-head-unreadable"
    assert str(tmp_path) not in str(excinfo.value)


def test_added_and_removed_actions_are_drift(tmp_path: Path) -> None:
    added_tools = {
        "resolve_control": ("get_version", "delete_everything", "restart_app"),
        "echo": ("back",),
    }
    baseline = _baseline(tmp_path, clone_tools=added_tools)
    with pytest.raises(McpToolSurfaceDriftError) as added:
        baseline.surface.validate_static()
    assert added.value.findings["action-added"] == ("resolve_control.restart_app",)

    removed_tools = {"resolve_control": ("get_version",), "echo": ("back",)}
    baseline = _baseline(tmp_path / "b", clone_tools=removed_tools)
    with pytest.raises(McpToolSurfaceDriftError) as removed:
        baseline.surface.validate_static()
    assert removed.value.findings["action-removed"] == ("resolve_control.delete_everything",)


def test_provider_version_change_is_drift(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path, version="9.9.9")
    with pytest.raises(McpToolSurfaceDriftError) as excinfo:
        baseline.surface.validate_static()
    assert excinfo.value.findings["provider-version"] == ("9.9.9", "2.98.3")


def test_added_removed_tool_and_changed_schema_are_drift(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    with_extra_tool = (*MINI_LIVE, LiveTool(name="brand_new", input_schema={"type": "object"}))
    with pytest.raises(McpToolSurfaceDriftError) as added:
        baseline.surface.validate_installed(with_extra_tool)
    assert added.value.findings["tool-added"] == ("brand_new",)

    with pytest.raises(McpToolSurfaceDriftError) as removed:
        baseline.surface.validate_installed(MINI_LIVE[:1])
    assert removed.value.findings["tool-removed"] == ("echo",)

    changed_schema = (
        LiveTool(
            name="resolve_control",
            input_schema={
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "x": 1,
            },
        ),
        MINI_LIVE[1],
    )
    with pytest.raises(McpToolSurfaceDriftError) as schema:
        baseline.surface.validate_installed(changed_schema)
    assert schema.value.findings["schema-changed"] == ("resolve_control",)


def test_drift_report_carries_only_ids_and_hashes(tmp_path: Path) -> None:
    poisoned = dict(_live_schema_hashes())
    poisoned["resolve_control"] = "a" * 64  # committed hash unrelated to live schema
    baseline = _baseline(tmp_path, schemas=poisoned)
    poisoned_live = (
        LiveTool(
            name="resolve_control",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": SECRET_SCHEMA_MARKER},
                    MEDIA_PATH_MARKER: {"type": "string"},
                },
            },
        ),
        MINI_LIVE[1],
        LiveTool(name="ghost_tool", input_schema={"type": "object"}),
    )
    with pytest.raises(McpToolSurfaceDriftError) as excinfo:
        baseline.surface.validate_installed(poisoned_live)
    text = str(excinfo.value)
    assert SECRET_SCHEMA_MARKER not in text
    assert MEDIA_PATH_MARKER not in text
    assert str(tmp_path) not in text
    assert excinfo.value.findings == {
        "schema-changed": ("resolve_control",),
        "tool-added": ("ghost_tool",),
    }
    assert re.fullmatch(r"[0-9a-f]{64}", excinfo.value.inventory_sha256)
    assert excinfo.value.code == "mcp-tool-surface-drift"


def test_committed_surface_status_ok_and_drift(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    ok = committed_surface_status(
        pin_path=baseline.pin_path,
        clone_dir=baseline.clone_dir,
        coverage_dir=baseline.coverage_dir,
    )
    assert ok.ok is True
    assert ok.code == "ok"
    drifted = _baseline(tmp_path / "b", pin_commit=OTHER_SHA)
    verdict = committed_surface_status(
        pin_path=drifted.pin_path,
        clone_dir=drifted.clone_dir,
        coverage_dir=drifted.coverage_dir,
    )
    assert verdict.ok is False
    assert verdict.code == "mcp-tool-surface-drift"
    assert "checkout-head" in verdict.detail


def test_gate_module_issues_no_tool_calls() -> None:
    """Drift validation is read-only: initialize/tools-list/static files only."""
    root = Path(__file__).resolve().parents[2] / "services" / "toolchain"
    for name in ("mcp_surface_gate.py", "mcp_surface_artifacts.py"):
        source = (root / name).read_text("utf-8")
        assert "_call_tool" not in source, name
        assert "_call_action_json" not in source, name
        assert "tools/call" not in source, name
