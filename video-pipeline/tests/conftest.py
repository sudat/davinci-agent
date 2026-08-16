from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--resolve-evidence",
        action="store",
        default=None,
        help="directory for live Resolve bridge evidence bundles",
    )
