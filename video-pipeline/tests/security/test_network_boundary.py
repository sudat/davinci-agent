"""Services-wide network-import boundary guard (task 3, v44-first-publish-delta).

AST-level proof (extending the ``tests/editorial/test_boundary.py`` pattern
to ALL of ``services/``): no module under ``services/`` imports a network
client (``urllib.request`` / ``http.client`` / ``socket`` / ``httpx`` /
``requests`` / ``aiohttp``) outside the sanctioned transport layer
``services/cli/`` (where ``live_editorial.py`` and ``live_editorial_v2.py``
live). ``urllib.parse`` performs no network I/O and is allowed anywhere
(precedent: ``services/review_command/translator.py``). The one recorded
exception is the local-only Resolve scripting-scope probe, whose every
socket targets the local host.

The v4.4 service modules (``editorial_v2/model_provider.py``,
``media_intelligence/moment_review_real.py``,
``episode_cockpit/review_interpreter.py``) must receive the injected
``HttpPost`` Protocol only — this walk enforces that for every file that
exists under ``services/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVICES_ROOT = Path("services")
CLI_LAYER = "services/cli"
#: Exactly enumerated current exceptions (verified 2026-08-23; do not grow
#: this set without recording why in the plan notepad):
#: - scripting_scope_probe.py — the LIVE local-only Resolve scripting-scope
#:   probe; every socket it opens targets the LOCAL host (loopback or this
#:   machine's own interface addresses), never external egress.
RECORDED_EXCEPTIONS = frozenset({
    "services/resolve_bridge/scripting_scope_probe.py",
})
#: Forbidden import roots anywhere under services/ outside the exceptions.
#: A bare ``urllib`` import is forbidden too (it grants lazy access to
#: urllib.request); ``urllib.parse`` is explicitly allowed.
FORBIDDEN_ROOTS = frozenset({"urllib", "http", "socket", "httpx", "requests", "aiohttp"})
_PARSE_ONLY_PREFIX = "urllib.parse"
#: Anchor files that MUST be visited — the walk can never silently pass on
#: an empty glob while any of these exist.
ANCHOR_FILES = (
    "services/cli/live_editorial.py",
    "services/cli/live_editorial_v2.py",
    "services/editorial_v2/model_provider.py",
    "services/review_command/translator.py",
    "services/resolve_bridge/scripting_scope_probe.py",
)


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _service_files() -> dict[str, str]:
    return {
        path.relative_to(SERVICES_ROOT.parent).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SERVICES_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_no_network_client_imports_outside_the_cli_layer() -> None:
    files = _service_files()
    for anchor in ANCHOR_FILES:
        assert anchor in files, f"guard anchor vanished: {anchor}"
        assert Path(anchor).is_file()
    for relative, source in files.items():
        if relative.startswith(CLI_LAYER + "/") or relative in RECORDED_EXCEPTIONS:
            continue
        imports = _imports(ast.parse(source, filename=relative))
        for name in sorted(imports):
            if name == _PARSE_ONLY_PREFIX or name.startswith(_PARSE_ONLY_PREFIX + "."):
                continue
            root = name.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{relative} imports network client {name!r} — real HTTP transports "
                f"live only under {CLI_LAYER}/; services receive an injected "
                "HttpPost transport instead"
            )


def test_recorded_exceptions_and_anchors_stay_real() -> None:
    """The exception set cannot rot: every recorded exception exists and the
    sanctioned CLI transports are actually present."""

    for relative in RECORDED_EXCEPTIONS:
        assert Path(relative).is_file(), f"recorded network exception vanished: {relative}"
    assert Path("services/cli/live_editorial_v2.py").is_file()
    # And the recorded exception really is a network importer (else the entry
    # is stale and must be deleted).
    probe_source = Path("services/resolve_bridge/scripting_scope_probe.py").read_text(
        encoding="utf-8"
    )
    assert "import socket" in probe_source


def test_urllib_parse_remains_allowed_anywhere() -> None:
    """Precedent check: the known urllib.parse-only consumer stays legal."""

    translator = _service_files()["services/review_command/translator.py"]
    imports = _imports(ast.parse(translator))
    urllib_imports = [name for name in imports if name.split(".")[0] == "urllib"]
    assert urllib_imports == ["urllib.parse"]
