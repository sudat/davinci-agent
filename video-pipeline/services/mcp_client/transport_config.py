"""Launch-spec value objects for the vendored MCP server subprocess.

Split from :mod:`services.mcp_client.transport` (module LOC ceiling): the
config side — the injectable command spec — while the transport module keeps
the process/IO machinery. The server is tracked as a plain vendored checkout
(``private/vendor/davinci-resolve-mcp``); there is no version-freeze contract,
so construction carries no pin provenance and no surface gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_KILL_GRACE_SECONDS: Final = 5.0
#: Name of the vendored checkout directory under ``<repo>/private/vendor``.
VENDOR_DIR_NAME: Final = "davinci-resolve-mcp"
#: Env override that keeps the vendored server from self-updating at spawn.
UPDATE_CHECK_OFF_ENV: Final = {"DAVINCI_RESOLVE_MCP_UPDATE_CHECK": "0"}


def default_vendor_dir() -> Path:
    """The vendored checkout dir, derived from this file's location.

    ``video-pipeline/services/mcp_client/transport_config.py`` → ``parents[3]``
    is the repo root, so no absolute path is hardcoded.
    """
    return (
        Path(__file__).resolve().parents[3]
        / "private"
        / "vendor"
        / VENDOR_DIR_NAME
    )


@dataclass(frozen=True, slots=True)
class StdioTransportConfig:
    """Injectable launch spec for the server subprocess."""

    command: tuple[str, ...]
    cwd: Path | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS

    @classmethod
    def from_defaults(
        cls,
        *,
        vendor_dir: Path | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> StdioTransportConfig:
        """Build the launch spec for the vendored server checkout.

        ``vendor_dir`` defaults to the repo's
        ``private/vendor/davinci-resolve-mcp`` checkout (derived from this
        file's location); pass it explicitly for any other deployment
        (tests inject a fixture directory).
        """
        resolved_vendor_dir = vendor_dir if vendor_dir is not None else default_vendor_dir()
        venv_python = resolved_vendor_dir / "venv" / "bin" / "python"
        return cls(
            command=(str(venv_python), "src/server.py"),
            cwd=resolved_vendor_dir,
            extra_env=dict(UPDATE_CHECK_OFF_ENV),
            request_timeout_seconds=request_timeout_seconds,
        )


__all__ = [
    "DEFAULT_KILL_GRACE_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "UPDATE_CHECK_OFF_ENV",
    "VENDOR_DIR_NAME",
    "StdioTransportConfig",
    "default_vendor_dir",
]
