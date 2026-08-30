"""Task 11 — the committed-surface gate is MANDATORY on pin-backed clients.

Regression suite for the failed-verification repair: constructing a client
from the pin (``McpClient.from_pin``) or from a pin-backed transport config
(``StdioTransportConfig.from_pin``) always applies the committed-surface
gate — there is no ``surface_gate=None`` opt-out. Manual (non-pin) configs
remain the unit-test path. A static snapshot pins every production
construction site so a new ungated one cannot appear silently.

The import-order tests run each hostile module order in a FRESH interpreter
(the in-process suite always imports the client first, which is exactly why
the mcp-doctor startup ImportError was invisible to the test run).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from services.mcp_client.client import McpClient
from services.mcp_client.errors import (
    McpClientError,
    McpSurfaceConfigError,
    McpToolSurfaceDriftError,
)
from services.mcp_client.transport import (
    DEFAULT_SURFACE_COVERAGE_DIR,
    McpTransportError,
    StdioJsonRpcTransport,
    StdioTransportConfig,
)
from services.toolchain.mcp_pin import load_mcp_pin
from tests.mcp_client.pin_backed_stub import (
    CANNED_TOOLS,
    GATE_TOOLS,
    write_stub_server,
)
from tests.toolchain.mcp_surface_support import (
    git_commit_clone,
    schema_hashes_for,
    write_coverage_pair,
    write_mini_clone,
    write_pin,
)


def _pin_backed_fixture(
    tmp_path: Path, *, served_tools: list[dict[str, object]] | None = None
) -> tuple[Path, Path, Path]:
    """Clone (static surface + live stub) + committed artifacts + pin file.

    The committed baseline always records the CANNED surface; ``served_tools``
    optionally drifts what the stub actually serves.
    """
    clone = write_mini_clone(tmp_path / "tree", GATE_TOOLS)
    write_stub_server(clone, served_tools if served_tools is not None else CANNED_TOOLS)
    head = git_commit_clone(clone)
    write_coverage_pair(
        tmp_path / "tree",
        pin_commit=head,
        schemas=schema_hashes_for(
            [
                {"name": tool["name"], "inputSchema": tool["inputSchema"]}
                for tool in CANNED_TOOLS
            ]
        ),
        actions=GATE_TOOLS,
    )
    pin = write_pin(tmp_path / "tree", commit=head, venv_python=sys.executable)
    return pin, clone, tmp_path / "tree" / "coverage"


def test_from_pin_auto_gates_and_exact_surface_is_usable(tmp_path: Path) -> None:
    pin, clone, coverage = _pin_backed_fixture(tmp_path)
    with McpClient.from_pin(
        pin, clone_dir=clone, coverage_dir=coverage, request_timeout_seconds=5.0
    ) as client:
        assert client.surface_gate() is not None
        identity = client.connect()
        assert identity.name == "DaVinciResolveMCP"
        assert client.get_server_info() == identity
        assert len(client.list_tools()) == len(CANNED_TOOLS)


def test_from_pin_auto_gate_rejects_drifted_live_surface(tmp_path: Path) -> None:
    drifted = [*CANNED_TOOLS, {"name": "ghost", "inputSchema": {"type": "object"}}]
    pin, clone, coverage = _pin_backed_fixture(tmp_path, served_tools=drifted)
    client = McpClient.from_pin(
        pin, clone_dir=clone, coverage_dir=coverage, request_timeout_seconds=5.0
    )
    with pytest.raises(McpToolSurfaceDriftError) as excinfo:
        client.connect()
    assert excinfo.value.findings["tool-added"] == ("ghost",)
    with pytest.raises(McpClientError):
        client.get_server_info()
    with pytest.raises(McpTransportError):
        client.resolve_get_version()
    client.close()


def test_direct_config_from_pin_client_also_gates(tmp_path: Path) -> None:
    pin_path, clone, coverage = _pin_backed_fixture(tmp_path)
    config = StdioTransportConfig.from_pin(
        load_mcp_pin(pin_path), clone_dir=clone, coverage_dir=coverage
    )
    with McpClient(StdioJsonRpcTransport(config)) as client:
        assert client.surface_gate() is not None
        client.connect()

    drifted = [tool for tool in CANNED_TOOLS if tool["name"] != "echo"]
    pin_path, clone, coverage = _pin_backed_fixture(tmp_path / "b", served_tools=drifted)
    config = StdioTransportConfig.from_pin(
        load_mcp_pin(pin_path), clone_dir=clone, coverage_dir=coverage
    )
    with McpClient(StdioJsonRpcTransport(config)) as client:
        with pytest.raises(McpToolSurfaceDriftError) as excinfo:
            client.connect()
        assert excinfo.value.findings["tool-removed"] == ("echo",)


def test_no_tools_call_occurs_before_the_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin, clone, coverage = _pin_backed_fixture(tmp_path)
    call_log = tmp_path / "wire-methods.log"
    monkeypatch.setenv("STUB_CALL_LOG", str(call_log))
    with McpClient.from_pin(
        pin, clone_dir=clone, coverage_dir=coverage, request_timeout_seconds=5.0
    ) as client:
        client.connect()
        client.list_tools()
    methods = call_log.read_text(encoding="utf-8").splitlines()
    assert "initialize" in methods
    assert "tools/list" in methods
    assert "tools/call" not in methods


def test_from_pin_with_missing_coverage_fails_closed_at_construction(
    tmp_path: Path,
) -> None:
    pin, clone, _ = _pin_backed_fixture(tmp_path)
    empty = tmp_path / "empty-coverage"
    empty.mkdir()
    with pytest.raises(McpSurfaceConfigError) as excinfo:
        McpClient.from_pin(pin, clone_dir=clone, coverage_dir=empty)
    assert excinfo.value.code == "surface-inventory-missing"


def test_manual_transport_stays_ungated_for_unit_tests() -> None:
    """The fake-server path (manual config, no pin provenance) still works."""
    config = StdioTransportConfig(command=("true",))
    client = McpClient(StdioJsonRpcTransport(config))
    assert client.surface_gate() is None
    client.close()


def test_default_coverage_dir_is_the_committed_tree() -> None:
    assert DEFAULT_SURFACE_COVERAGE_DIR.name == "mcp-coverage"
    assert (DEFAULT_SURFACE_COVERAGE_DIR / "inventory.json").is_file()


def test_client_source_has_no_surface_gate_opt_out() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "services" / "mcp_client" / "client.py"
    ).read_text("utf-8")
    assert "surface_gate=" not in source
    assert "surface_gate:" not in source
    assert "skip_validation" not in source


# ─── static snapshot: every production construction site ─────────────────────

_SITE_PATTERN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("McpClient.from_pin", re.compile(r"\bMcpClient\.from_pin\(")),
    ("McpDiscoveryClient.from_pin", re.compile(r"\bMcpDiscoveryClient\.from_pin\(")),
    ("McpClient()", re.compile(r"\bMcpClient\(")),
    ("McpDiscoveryClient()", re.compile(r"\bMcpDiscoveryClient\(")),
    ("StdioJsonRpcTransport()", re.compile(r"\bStdioJsonRpcTransport\(")),
    ("StdioTransportConfig.from_pin", re.compile(r"\bStdioTransportConfig\.from_pin\(")),
    ("StdioTransportConfig()", re.compile(r"\bStdioTransportConfig\(")),
)

_EXPECTED_SITES: Mapping[str, frozenset[str]] = {
    # definition sites + their internal pin-backed constructors (classmethod
    # DEFINITIONS do not contain the literal "Cls.from_pin(" — only calls do)
    "mcp_client/client.py": frozenset(
        {"StdioJsonRpcTransport()", "StdioTransportConfig.from_pin"}
    ),
    "mcp_client/discovery.py": frozenset(
        {"StdioJsonRpcTransport()", "StdioTransportConfig()"}
    ),
    # pin-backed product paths (all auto-gated via StdioTransportConfig.from_pin)
    "cli/episode0.py": frozenset({"McpClient.from_pin"}),
    "cli/mcp_inventory.py": frozenset({"McpDiscoveryClient.from_pin"}),
    "qa/parity_mcp.py": frozenset(
        {
            "McpClient()",
            "StdioJsonRpcTransport()",
            "StdioTransportConfig.from_pin",
        }
    ),
}


def test_production_mcp_client_construction_sites_snapshot() -> None:
    services = Path(__file__).resolve().parents[2] / "services"
    found: dict[str, set[str]] = {}
    for path in sorted(services.rglob("*.py")):
        source = path.read_text("utf-8")
        hits = {
            name
            for name, pattern in _SITE_PATTERN
            if pattern.search(source)
        }
        if hits:
            found[path.relative_to(services).as_posix()] = hits
    assert found == {key: set(value) for key, value in _EXPECTED_SITES.items()}


def test_connect_ordering_runs_gate_after_tools_list_before_usable_state() -> None:
    """Source-pinned ordering: handshake → tools/list → gate → connected."""
    source = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "mcp_client"
        / "client.py"
    ).read_text("utf-8")
    connect_body = source[source.index("def connect") : source.index("def get_server_info")]
    handshake = connect_body.index("verify_server_identity")
    notification = connect_body.index("notifications/initialized")
    gate = connect_body.index("validate_installed")
    usable = connect_body.index("self._server_info = handshake.server_info")
    assert handshake < notification < gate < usable


def test_discovery_client_exposes_no_tool_call_seam() -> None:
    """Generation's read-only path: initialize/tools-list/close capabilities only."""
    discovery_source = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "mcp_client"
        / "discovery.py"
    ).read_text("utf-8")
    assert "_call_tool" not in discovery_source
    assert "_call_action_json" not in discovery_source
    assert "tools/call" not in discovery_source
    inventory_source = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "cli"
        / "mcp_inventory.py"
    ).read_text("utf-8")
    assert "McpClient" not in inventory_source
    assert "McpDiscoveryClient" in inventory_source


# ─── import-order regression: the mcp-doctor startup ImportError ─────────────
#
# client.py must not import mcp_surface_gate at module level: the gate imports
# services.mcp_client.errors, whose package __init__ imports client back — a
# gate-first import order (mcp-doctor) then hits a partially initialized gate.
# Each order below runs in its own interpreter because the in-process suite
# has already imported the client by collection time.

_IMPORT_ORDERS: tuple[tuple[str, str], ...] = (
    (
        "doctor-entrypoint",
        "import services.cli.mcp_doctor",
    ),
    (
        "gate-first",
        "import services.toolchain.mcp_surface_gate; import services.mcp_client",
    ),
    (
        "artifacts-first",
        ("import services.toolchain.mcp_surface_artifacts; "
         "import services.mcp_client.client"),
    ),
    (
        "client-first",
        "import services.mcp_client; import services.toolchain.mcp_surface_gate",
    ),
)


@pytest.mark.parametrize(("order_id", "code"), _IMPORT_ORDERS, ids=[o[0] for o in _IMPORT_ORDERS])
def test_hostile_import_orders_start_clean_in_fresh_interpreters(
    order_id: str, code: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"import order {order_id!r} failed in a fresh interpreter:\n{completed.stderr}"
    )


def test_doctor_entrypoint_reaches_checks_in_fresh_interpreter() -> None:
    """The exact failing command shape: runpy ``-m services.cli.mcp_doctor``."""
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.mcp_doctor",
            "--clone",
            str(root / "does-not-exist"),
            "--pin",
            str(root / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"),
            "--repo-venv",
            str(root / ".venv" / "bin" / "python"),
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "ImportError" not in completed.stderr
    assert "Traceback" not in completed.stderr
    # A nonexistent clone is a typed doctor FAILURE (JSON report, exit 1) —
    # proof the module reached its checks instead of dying at import time.
    assert completed.returncode == 1
    assert completed.stdout.strip().startswith("{")
    assert '"schema_version": "mcp-doctor-v1"' in completed.stdout
