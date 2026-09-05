"""Node runtime check seam of ``mcp-doctor``.

The advanced server (``davinci-resolve-advanced-mcp``) runs on Node, and the
vendor's v2.207.0 ``package.json`` pins ``engines.node >= 20.9`` — a floor the
current pin's 18.17 does not enforce. The doctor therefore verifies the
interpreter before any cutover lands on a machine with an old Node.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import Final

from services.cli.mcp_doctor_report import DoctorSection, fail_section, ok_section

NODE_MINIMUM: Final[tuple[int, int, int]] = (20, 9, 0)
NODE_MINIMUM_TEXT: Final = "20.9"
_VERSION_TIMEOUT_SECONDS: Final = 10.0
_VERSION_RE: Final = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].+)?$")

NodeLookup = Callable[[], str | None]


def parse_node_version(output: str) -> tuple[int, int, int] | None:
    """``v24.19.0`` → ``(24, 19, 0)``; build/prerelease suffixes are ignored.

    Anything else (empty, missing ``v``-prefix garbage, partial versions) is
    ``None`` so callers fail closed instead of guessing.
    """
    match = _VERSION_RE.match(output.strip())
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def default_node_lookup() -> str | None:
    """Run ``node --version`` (absent binary → ``None``)."""
    try:
        completed = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def check_node(lookup: NodeLookup) -> DoctorSection:
    found = lookup()
    if found is None:
        return fail_section(
            "node_version",
            "node-missing",
            f"node not found; the advanced server requires Node >= {NODE_MINIMUM_TEXT}",
        )
    version = parse_node_version(found)
    if version is None:
        return fail_section(
            "node_version",
            "node-version-unreadable",
            f"could not parse node version from {found.strip()!r}"
            f" (requires >= {NODE_MINIMUM_TEXT})",
        )
    if version < NODE_MINIMUM:
        return fail_section(
            "node_version",
            "node-version-too-old",
            f"node {version[0]}.{version[1]}.{version[2]} is older than the required"
            f" >= {NODE_MINIMUM_TEXT}",
        )
    return ok_section("node_version", f"node {found.strip()} satisfies >= {NODE_MINIMUM_TEXT}")


__all__ = [
    "NODE_MINIMUM",
    "NODE_MINIMUM_TEXT",
    "NodeLookup",
    "check_node",
    "default_node_lookup",
    "parse_node_version",
]
