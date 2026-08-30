"""``mcp-inventory`` — generate the pinned MCP operation inventory.

Read-only coverage visibility (Task 9): validates the pin, the local vendor
checkout HEAD, the provider version, the server mode, and the advanced-enabled
state; then captures the pinned compound server's ``tools/list`` via the
read-only :class:`McpDiscoveryClient` (initialize + tools/list + close ONLY —
no tool-call seam exists on that class, so no project/timeline/media/render
mutation is possible; it is deliberately ungated because generation must
inspect a changed pin BEFORE the new inventory exists) and statically
cross-checks the vendor granular tool definitions and the guarded kernel
catalog. Output is the deterministic ``mcp-inventory-v1`` tree under
``capabilities/mcp-coverage/`` plus a ``manifest-v1`` content manifest.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Final

from services.foundation_io import atomic_write
from services.mcp_client.discovery import McpDiscoveryClient
from services.release.manifest import MANIFEST_NAME, build_manifest, manifest_bytes
from services.toolchain.mcp_coverage import (
    LiveTool,
    McpCoverageError,
    PinFacts,
    build_inventory,
    inventory_bytes,
    inventory_sha256,
)
from services.toolchain.mcp_fit import EXPECTED_PROVIDER_VERSION
from services.toolchain.mcp_pin import McpPinError, load_mcp_pin
from services.toolchain.mcp_vendor_surface import (
    VendorSurfaceError,
    parse_vendor_surface,
)

GIT_TIMEOUT_SECONDS: Final = 10.0
REQUEST_TIMEOUT_SECONDS: Final = 60.0


class InventoryCommandError(Exception):
    """A typed generation failure with an exit-worthy code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _check_head(clone_dir: Path, pinned_commit: str) -> None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise InventoryCommandError("checkout-head-unreadable", str(exc)) from exc
    if completed.returncode != 0:
        raise InventoryCommandError(
            "checkout-head-unreadable", completed.stderr.strip() or "git rev-parse failed"
        )
    head = completed.stdout.strip()
    if head != pinned_commit:
        raise InventoryCommandError(
            "checkout-head-mismatch",
            f"vendor checkout HEAD {head} != pinned commit {pinned_commit}",
        )


def _check_advanced(clone_dir: Path, *, advanced_enabled: bool) -> None:
    if not advanced_enabled:
        return
    binary = clone_dir / "bin" / "davinci-resolve-advanced-mcp.mjs"
    package = clone_dir / "resolve-advanced" / "package.json"
    if not binary.is_file() and not package.is_file():
        raise InventoryCommandError(
            "advanced-missing", f"neither {binary} nor {package} exists"
        )


def capture_live_tools(
    pin_path: Path, clone_dir: Path
) -> tuple[tuple[str, str], tuple[LiveTool, ...]]:
    """Connect read-only and capture ``tools/list`` (no tool-call capability)."""
    discovery = McpDiscoveryClient.from_pin(
        pin_path, clone_dir=clone_dir, request_timeout_seconds=REQUEST_TIMEOUT_SECONDS
    )
    try:
        identity = discovery.connect()
        tools = discovery.list_tools()
    finally:
        discovery.close()
    live = tuple(
        LiveTool(name=tool.name, input_schema=tool.input_schema)
        for tool in sorted(tools, key=lambda candidate: candidate.name)
    )
    return (identity.name, identity.version), live


def generate_inventory(pin_path: Path, clone_dir: Path) -> tuple[bytes, str]:
    """Full generation: pin/HEAD/provider/mode/advanced checks + inventory bytes."""
    try:
        pin = load_mcp_pin(pin_path)
    except McpPinError as exc:
        raise InventoryCommandError("pin-invalid", str(exc)) from exc
    _check_head(clone_dir, pin.commit)
    try:
        surface = parse_vendor_surface(clone_dir)
    except VendorSurfaceError as exc:
        raise InventoryCommandError(exc.code, exc.detail) from exc
    if surface.provider_version != EXPECTED_PROVIDER_VERSION:
        raise InventoryCommandError(
            "provider-version-mismatch",
            f"vendor VERSION {surface.provider_version} != expected"
            f" {EXPECTED_PROVIDER_VERSION}",
        )
    _check_advanced(clone_dir, advanced_enabled=pin.advanced_server.enabled)
    handshake, live_tools = capture_live_tools(pin_path, clone_dir)
    facts = PinFacts(
        commit=pin.commit,
        provider_version=surface.provider_version,
        server_mode=pin.server_mode,
        advanced_enabled=pin.advanced_server.enabled,
        handshake_name=handshake[0],
        handshake_version=handshake[1],
    )
    try:
        inventory = build_inventory(facts, surface, live_tools)
    except McpCoverageError as exc:
        raise InventoryCommandError(exc.code, exc.detail) from exc
    return inventory_bytes(inventory), inventory_sha256(inventory)


def write_inventory_tree(out_dir: Path, payload: bytes) -> None:
    """Write ``inventory.json`` + the ``manifest-v1`` content manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = out_dir / "inventory.json"
    atomic_write(inventory_path, payload)
    atomic_write(out_dir / MANIFEST_NAME, manifest_bytes(build_manifest(out_dir)))
    for path in (inventory_path, out_dir / MANIFEST_NAME):
        path.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    video_pipeline_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="mcp-inventory", description=__doc__)
    parser.add_argument(
        "--pin",
        type=Path,
        default=video_pipeline_root / "config" / "toolchains" / "davinci-resolve-mcp.pin.json",
    )
    parser.add_argument(
        "--clone",
        type=Path,
        default=video_pipeline_root.parent / "private" / "vendor" / "davinci-resolve-mcp",
    )
    parser.add_argument(
        "--out", type=Path, default=video_pipeline_root / "capabilities" / "mcp-coverage"
    )
    args = parser.parse_args(argv)
    try:
        payload, digest = generate_inventory(args.pin, args.clone)
        write_inventory_tree(args.out, payload)
    except InventoryCommandError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "inventory": str(args.out / "inventory.json"),
                "manifest": str(args.out / MANIFEST_NAME),
                "inventory_sha256": digest,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

__all__ = [
    "InventoryCommandError",
    "capture_live_tools",
    "generate_inventory",
    "main",
    "write_inventory_tree",
]
