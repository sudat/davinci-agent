from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--resolve-evidence",
        action="store",
        default=None,
        help="directory for live Resolve bridge evidence bundles",
    )
    parser.addoption(
        "--exclusive-resolve-lease",
        action="store_true",
        default=False,
        help="live Resolve tests acquire an exclusive lease; a held lease skips them",
    )
