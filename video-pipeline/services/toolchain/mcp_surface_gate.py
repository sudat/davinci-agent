"""Fail-closed gate over the committed MCP surface (Task 11).

The Task 9 inventory — sealed by the Task 10 dispositions and the tree
manifest — is the committed truth for the pinned MCP surface. This module
loads that baseline (see :mod:`services.toolchain.mcp_surface_artifacts`)
and compares the INSTALLED surface against the commit:

- the vendor checkout HEAD must equal the pin commit (a wrong checkout
  fails even when the running server reports the pinned identity);
- the statically parsed compound action sets must match the inventory
  (the wire ``tools/list`` schema carries no action enum, so action truth
  comes from the pinned local ``src/server.py`` parser);
- the live ``tools/list`` names and canonical input-schema SHA-256 hashes
  must match the inventory.

Drift reports carry ONLY sorted IDs and safe shape identifiers (commits,
hashes, counts) — never raw schemas, secrets, environment values, media
data, or absolute paths.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_client.errors import McpSurfaceConfigError, McpToolSurfaceDriftError
from services.toolchain.mcp_coverage import schema_sha256
from services.toolchain.mcp_pin import McpPinError, load_mcp_pin
from services.toolchain.mcp_surface_artifacts import (
    SurfaceFacts,
    load_surface_facts,
)
from services.toolchain.mcp_vendor_surface import (
    VendorSurface,
    VendorSurfaceError,
    parse_vendor_surface,
)

if TYPE_CHECKING:
    from services.toolchain.mcp_coverage_models import LiveTool
    from services.toolchain.mcp_pin import McpPin

GIT_TIMEOUT_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class CommittedSurface:
    """A loaded, artifact-agreed baseline for drift validation."""

    facts: SurfaceFacts
    clone_dir: Path

    @property
    def pin_commit(self) -> str:
        return self.facts.pin_commit

    @property
    def inventory_sha256(self) -> str:
        return self.facts.inventory_sha256

    def validate_static(self) -> None:
        """Checkout HEAD + statically parsed vendor actions vs the inventory."""
        self._raise_if_drift(self._static_findings())

    def validate_installed(self, live_tools: tuple[LiveTool, ...]) -> None:
        """Everything installed: checkout, static action sets, live tools/list."""
        findings = self._static_findings()
        findings.update(self._live_findings(live_tools))
        self._raise_if_drift(findings)

    def _static_findings(self) -> dict[str, tuple[str, ...]]:
        findings: dict[str, tuple[str, ...]] = {}
        head = self._checkout_head()
        if head != self.facts.pin_commit:
            findings["checkout-head"] = (head, self.facts.pin_commit)
        surface = self._parse_vendor()
        if surface.provider_version != self.facts.provider_version:
            findings["provider-version"] = (
                surface.provider_version,
                self.facts.provider_version,
            )
        static_actions = {
            tool.name: tuple(sorted(tool.actions)) for tool in surface.compound_tools
        }
        added = sorted(
            f"{tool}.{action}"
            for tool, actions in static_actions.items()
            for action in actions
            if action not in self.facts.tool_actions.get(tool, ())
        )
        if added:
            findings["action-added"] = tuple(added)
        removed = sorted(
            f"{tool}.{action}"
            for tool, actions in self.facts.tool_actions.items()
            for action in actions
            if action not in static_actions.get(tool, ())
        )
        if removed:
            findings["action-removed"] = tuple(removed)
        return findings

    def _live_findings(
        self, live_tools: tuple[LiveTool, ...]
    ) -> dict[str, tuple[str, ...]]:
        findings: dict[str, tuple[str, ...]] = {}
        live_names = {tool.name for tool in live_tools}
        expected = self.facts.tool_schema_sha256
        added = sorted(live_names - expected.keys())
        if added:
            findings["tool-added"] = tuple(added)
        removed = sorted(expected.keys() - live_names)
        if removed:
            findings["tool-removed"] = tuple(removed)
        changed = sorted(
            tool.name
            for tool in live_tools
            if tool.name in expected
            and schema_sha256(tool.input_schema) != expected[tool.name]
        )
        if changed:
            findings["schema-changed"] = tuple(changed)
        return findings

    def _raise_if_drift(self, findings: dict[str, tuple[str, ...]]) -> None:
        if findings:
            raise McpToolSurfaceDriftError(
                findings,
                pin_commit=self.facts.pin_commit,
                inventory_sha256=self.facts.inventory_sha256,
            )

    def _checkout_head(self) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.clone_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise _head_unreadable() from exc
        if completed.returncode != 0:
            raise _head_unreadable()
        return completed.stdout.strip()

    def _parse_vendor(self) -> VendorSurface:
        try:
            return parse_vendor_surface(self.clone_dir)
        except VendorSurfaceError as exc:
            raise McpSurfaceConfigError(
                "surface-vendor-unparsable", f"vendor checkout static parse failed ({exc.code})"
            ) from exc


def _head_unreadable() -> McpSurfaceConfigError:
    return McpSurfaceConfigError(
        "surface-checkout-head-unreadable",
        "git rev-parse HEAD failed on the vendor checkout",
    )


def load_committed_surface(
    *, pin: McpPin, clone_dir: Path, coverage_dir: Path
) -> CommittedSurface:
    """Load the committed triple (typed config errors) and bind the checkout."""
    return CommittedSurface(
        facts=load_surface_facts(pin=pin, coverage_dir=coverage_dir),
        clone_dir=clone_dir,
    )


@dataclass(frozen=True, slots=True)
class SurfaceGateStatus:
    """Doctor-facing verdict of the offline committed-surface check."""

    ok: bool
    code: str
    detail: str


def committed_surface_status(
    *, pin_path: Path, clone_dir: Path, coverage_dir: Path
) -> SurfaceGateStatus:
    """Offline gate (no server spawn): artifacts agree + checkout matches."""
    try:
        pin = load_mcp_pin(pin_path)
    except McpPinError as exc:
        return SurfaceGateStatus(
            ok=False, code="surface-pin-invalid", detail=str(exc)
        )
    try:
        surface = load_committed_surface(
            pin=pin, clone_dir=clone_dir, coverage_dir=coverage_dir
        )
        surface.validate_static()
    except McpSurfaceConfigError as exc:
        return SurfaceGateStatus(ok=False, code=exc.code, detail=exc.detail)
    except McpToolSurfaceDriftError as exc:
        return SurfaceGateStatus(ok=False, code=exc.code, detail=str(exc))
    return SurfaceGateStatus(
        ok=True,
        code="ok",
        detail=f"inventory {surface.inventory_sha256[:12]} sealed at pin"
        f" {surface.pin_commit[:12]}; checkout and static actions match",
    )


__all__ = [
    "CommittedSurface",
    "SurfaceGateStatus",
    "committed_surface_status",
    "load_committed_surface",
]
