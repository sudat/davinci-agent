"""``python -m services.cli`` — the typed local operator CLI entrypoint."""

from __future__ import annotations

from services.cli.operator import main

if __name__ == "__main__":
    raise SystemExit(main())
