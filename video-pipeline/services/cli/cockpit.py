"""``cockpit`` — serve the loopback-only episode cockpit web API (task 44).

Thin launcher: parses paths/port, delegates to
``services.episode_cockpit.app.run`` (which refuses any non-loopback host
with a typed error). State stays in the existing job-runner StateStore and
episode workspace — this command only serves.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.episode_cockpit.app import (
    DEFAULT_PORT,
    LOOPBACK_HOST,
    CockpitBindError,
    run,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli cockpit",
        description="Serve the loopback-only episode cockpit API.",
    )
    parser.add_argument("--episodes-root", type=Path, default=Path("jobs"))
    parser.add_argument(
        "--state-store",
        type=Path,
        default=None,
        help="StateStore SQLite path (default: <episodes-root>/state.db)",
    )
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    state_store = (
        arguments.state_store
        if arguments.state_store is not None
        else arguments.episodes_root / "state.db"
    )
    try:
        run(
            state_store_path=state_store,
            episodes_root=arguments.episodes_root,
            host=arguments.host,
            port=arguments.port,
        )
    except CockpitBindError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
