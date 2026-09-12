"""Pin-backed stub server for surface-gate binding tests.

Composes ONE ``src/server.py`` that is both things the gate needs: the
statically parseable compound surface (Task 9 parser reads the
``@mcp.tool`` defs before the loop) and a live stdio JSON-RPC responder
serving the baked ``tools/list`` payload. The loop only ends when stdin
closes, so the static block never executes. Every received JSON-RPC method
name is appended to the file named by the ``STUB_CALL_LOG`` environment
variable (when set), so a test can prove exactly which wire methods were
attempted by the client under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.toolchain.mcp_surface_support import static_server_source

GATE_TOOLS: dict[str, tuple[str, ...]] = {
    "resolve_control": ("get_version", "delete_everything"),
    "echo": ("back",),
}

CANNED_TOOLS: list[dict[str, object]] = [
    {
        "name": "resolve_control",
        "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}},
    },
    {"name": "echo", "inputSchema": {"type": "object"}},
]

_LOOP_TEMPLATE = """

import json as _json
import os as _os
import sys as _sys

_TOOLS = _json.loads({tools_json!r})
_LOG = _os.environ.get("STUB_CALL_LOG")


def _send(payload):
    _sys.stdout.write(_json.dumps(payload) + "\\n")
    _sys.stdout.flush()


def _log(method):
    if _LOG:
        with open(_LOG, "a") as handle:
            handle.write(method + "\\n")


for _line in _sys.stdin:
    _line = _line.strip()
    if not _line:
        continue
    try:
        _request = _json.loads(_line)
    except _json.JSONDecodeError:
        continue
    _rid = _request.get("id")
    if _rid is None:
        continue
    _method = _request.get("method")
    _log(_method)
    if _method == "initialize":
        _send({{"jsonrpc": "2.0", "id": _rid, "result": {{
            "protocolVersion": "2024-11-05",
            "capabilities": {{"tools": {{}}}},
            "serverInfo": {{"name": "DaVinciResolveMCP", "version": "1.30.0"}},
        }}}})
    elif _method == "tools/list":
        _send({{"jsonrpc": "2.0", "id": _rid, "result": {{"tools": _TOOLS}}}})
"""


def write_stub_server(clone_dir: Path, tools_payload: list[dict[str, object]]) -> None:
    """Overwrite the clone's entry point with the dual-purpose stub.

    The stdio loop comes FIRST; the statically parseable surface block sits
    AFTER the loop, so it never executes (the loop ends only at stdin EOF)
    while the Task 9 parser still reads its ``@mcp.tool`` definitions.
    """
    (clone_dir / "src" / "server.py").write_text(
        _LOOP_TEMPLATE.format(tools_json=json.dumps(tools_payload))
        + "\n\n"
        + static_server_source(GATE_TOOLS),
        encoding="utf-8",
    )
