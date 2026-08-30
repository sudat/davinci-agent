"""Launch-spec value objects for the pinned MCP server subprocess.

Split from :mod:`services.mcp_client.transport` (module LOC ceiling): the
config side — the injectable command spec plus the typed pin provenance
that makes a construction pin-backed and therefore surface-gated — while
the transport module keeps the process/IO machinery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from services.toolchain.mcp_pin import McpPin

DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_KILL_GRACE_SECONDS: Final = 5.0
#: Where the committed Task 9/10 coverage artifacts live; the surface gate
#: validates every pin-backed connection against this tree by default.
DEFAULT_SURFACE_COVERAGE_DIR: Final = (
    Path(__file__).resolve().parents[2] / "capabilities" / "mcp-coverage"
)


@dataclass(frozen=True, slots=True)
class PinSurfaceContext:
    """Typed pin provenance carried by every pin-backed transport config.

    Presence of this context is what makes a client construction pin-backed:
    :class:`services.mcp_client.client.McpClient` loads the committed
    surface gate from it eagerly, so no pin-backed client can become usable
    without committed-surface validation. Manual/test configs omit it.
    """

    pin: McpPin
    clone_dir: Path
    coverage_dir: Path


@dataclass(frozen=True, slots=True)
class StdioTransportConfig:
    """Injectable launch spec for the server subprocess."""

    command: tuple[str, ...]
    cwd: Path | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS
    #: Set ONLY by :meth:`from_pin` (or explicitly by a test injecting fixture
    #: artifacts): the pin provenance that auto-arms the surface gate.
    surface: PinSurfaceContext | None = None

    @classmethod
    def from_pin(
        cls,
        pin: McpPin,
        *,
        clone_dir: Path | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        coverage_dir: Path | None = None,
    ) -> StdioTransportConfig:
        """Build the launch spec from the pin contract (task 2).

        ``clone_dir`` defaults to the pinned venv's grandparent directory
        (``<clone>/venv/bin/python`` -> ``<clone>``), the layout the pin
        records; pass it explicitly for any other deployment. The config
        always carries the pin surface context, so every client built from
        it validates against the committed coverage artifacts
        (``coverage_dir`` defaults to the committed tree; tests inject a
        fixture directory).
        """
        venv_python = Path(pin.venv_python)
        resolved_clone_dir = (
            clone_dir if clone_dir is not None else venv_python.parent.parent.parent
        )
        return cls(
            command=(pin.venv_python, pin.server_entry_point),
            cwd=resolved_clone_dir,
            extra_env={"DAVINCI_RESOLVE_MCP_UPDATE_CHECK": "0"},
            request_timeout_seconds=request_timeout_seconds,
            surface=PinSurfaceContext(
                pin=pin,
                clone_dir=resolved_clone_dir,
                coverage_dir=(
                    coverage_dir if coverage_dir is not None
                    else DEFAULT_SURFACE_COVERAGE_DIR
                ),
            ),
        )


__all__ = [
    "DEFAULT_KILL_GRACE_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_SURFACE_COVERAGE_DIR",
    "PinSurfaceContext",
    "StdioTransportConfig",
]
