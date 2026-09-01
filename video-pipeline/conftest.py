from __future__ import annotations

import locale
from collections.abc import Iterator

import pytest

from tests.historical_artifact_quarantine import (
    HISTORICAL_ARTIFACT_NODE_ID_SET,
    HISTORICAL_ARTIFACT_SKIP_REASON,
)

#: Process locale captured at collection start — the state every test must
#: both begin and end with. The DaVinci Resolve native bridge flips the C
#: locale to "C"/US-ASCII when it loads in-process; without a per-test
#: boundary that leak poisons every later default-encoding text decode.
_BASELINE_LOCALE = locale.setlocale(locale.LC_ALL)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.nodeid in HISTORICAL_ARTIFACT_NODE_ID_SET:
            item.add_marker(pytest.mark.skip(reason=HISTORICAL_ARTIFACT_SKIP_REASON))


@pytest.fixture(autouse=True)
def _isolate_process_locale() -> Iterator[None]:
    drifted = locale.setlocale(locale.LC_ALL)
    assert drifted == _BASELINE_LOCALE, (
        f"process locale drifted to {drifted!r} before this test;"
        " a previous test or fixture leaked a native-bridge locale change"
    )
    yield
    locale.setlocale(locale.LC_ALL, _BASELINE_LOCALE)


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
